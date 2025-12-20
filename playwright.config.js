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
    command: "flask --app app run --port 5000",
    url: "http://localhost:5000",
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
    env: {
      EMAIL_MODE: "console",
      FLASK_APP: "app",
      FLASK_ENV: "development",
      FLASK_RUN_PORT: "5000",
    },
  },
});
