const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests",
  timeout: 30000,
  retries: 0,
  use: {
    baseURL: "http://localhost:5000",
    headless: true,
    trace: "retain-on-failure",
  },
  webServer: {
    command:
      "python -c \"import os; db='sorty_ui_test.db'; os.path.exists(db) and os.remove(db); from app import app; app.run(host='127.0.0.1', port=5000)\"",
    url: "http://localhost:5000",
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
    env: {
      EMAIL_MODE: "console",
      WHATSAPP_MODE: "console",
      DATABASE_URL: "sqlite:///sorty_ui_test.db",
    },
  },
});
