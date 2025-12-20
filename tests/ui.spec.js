const { test, expect } = require("@playwright/test");

test.describe("Participantes - escritorio", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("agregar, editar y eliminar mantiene un solo administrador", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("Agrega nombre, email y marca un Administrador")).toBeVisible();

    // Primer participante como administrador.
    const addBtn = page.locator("#participant-form button[type='submit']");

    await page.fill("#name", "Alice");
    await page.fill("#email", "alice@example.com");
    await page.locator("#admin-wrapper").click();
    await addBtn.click();

    const rows = page.locator("#participants-body tr");
    await expect(rows).toHaveCount(1);
    await expect(rows.nth(0).locator('input[type="radio"]')).toBeChecked();

    // Segundo participante, sin admin.
    await page.fill("#name", "Bob");
    await page.fill("#email", "bob@example.com");
    await addBtn.click();
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0).locator('input[type="radio"]')).toBeChecked();
    await expect(rows.nth(1).locator('input[type="radio"]')).not.toBeChecked();

    // Editar segundo.
    await rows.nth(1).getByRole("button", { name: "Editar" }).click();
    await expect(page.locator("#name")).toHaveValue("Bob");
    await page.fill("#name", "Bob Updated");
    await page.getByRole("button", { name: "Guardar cambios" }).click();
    await expect(rows.nth(1)).toContainText("Bob Updated");
    await expect(rows.nth(0).locator('input[type="radio"]')).toBeChecked();

    // Eliminar segundo.
    await rows.nth(1).getByRole("button", { name: "Eliminar" }).click();
    await expect(rows).toHaveCount(1);
    await expect(rows.nth(0)).toContainText("Alice");
  });
});

test.describe("Participantes - mobile", () => {
  test.use({ viewport: { width: 430, height: 900 } });

  test("muestra iconos de editar y eliminar en modo mobile", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("Agrega nombre, email y marca un Administrador")).toBeVisible();

    const addBtn = page.locator("#participant-form button[type='submit']");

    await page.fill("#name", "Mobile One");
    await page.fill("#email", "one@example.com");
    await page.locator("#admin-wrapper").click();
    await addBtn.click();

    await page.fill("#name", "Mobile Two");
    await page.fill("#email", "two@example.com");
    await addBtn.click();

    const actions = page.locator("#participants-body .table-actions .table-btn.icon");
    // Dos iconos por fila (editar + eliminar).
    await expect(actions).toHaveCount(4);
    await expect(actions.nth(0)).toBeVisible();
    await expect(actions.nth(1)).toBeVisible();
  });
});

test.describe("Reglas de sorteo", () => {
  test("requiere minimo tres participantes antes de sortear", async ({ page }) => {
    await page.goto("/");
    const addBtn = page.locator("#participant-form button[type='submit']");
    await page.fill("#name", "Uno");
    await page.fill("#email", "uno@example.com");
    await page.locator("#admin-wrapper").click();
    await addBtn.click();

    await page.fill("#name", "Dos");
    await page.fill("#email", "dos@example.com");
    await addBtn.click();

    await page.getByRole("button", { name: "Enviar correos" }).click();
    await expect(page.getByText("Carga al menos tres personas.")).toBeVisible();
    await expect(page.locator("#toast")).toHaveClass(/(?<!show)/, { timeout: 4000 });

    await page.fill("#name", "Tres");
    await page.fill("#email", "tres@example.com");
    await addBtn.click();

    await page.getByRole("button", { name: "Enviar correos" }).click();
    await expect(page.locator("#toast")).not.toHaveClass(/show/, { timeout: 4000 });
  });
});
