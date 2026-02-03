(function () {
  const themeKey = "sorty-theme";
  const themeToggle = document.getElementById("theme-toggle");

  function applyTheme(theme) {
    const next = theme === "dark" ? "dark" : "light";
    document.body.classList.toggle("theme-dark", next === "dark");
    localStorage.setItem(themeKey, next);
  }

  applyTheme(localStorage.getItem(themeKey) || "light");

  if (themeToggle) {
    themeToggle.addEventListener("click", () => {
      const isDark = document.body.classList.contains("theme-dark");
      applyTheme(isDark ? "light" : "dark");
    });
  }

  const revealTargets = document.querySelectorAll(
    ".landing-hero, .landing-section, .landing-cta"
  );
  if (!("IntersectionObserver" in window)) {
    revealTargets.forEach((node) => node.classList.add("is-visible"));
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    },
    { threshold: 0.16 }
  );

  revealTargets.forEach((node) => observer.observe(node));
})();
