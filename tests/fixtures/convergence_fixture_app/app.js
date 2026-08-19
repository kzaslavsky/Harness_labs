// Convergence fixture app (CC-03 static fixture). Applies the theme
// requested via the `theme` query parameter by copying each element's
// `data-style-<theme>` attribute onto its live `style`, wires the
// `#menu-toggle` interaction, and keeps the text of any element carrying
// `data-dynamic-interval-ms="<ms>"` changing on that interval so a real
// browser's two end-state reads legitimately disagree. This is a general
// convention, not tied to any one route or element id: any fixture page may
// declare a dynamic element this way. The capture script's stub driver
// never executes this file (it does not run a JS engine); it emulates the
// same conventions structurally by reading the `data-style-*` /
// `data-dynamic-interval-ms` markers directly and recomputing text from
// real elapsed time.
(function () {
  function applyThemeStyles(theme) {
    document.querySelectorAll("[data-style-" + theme + "]").forEach(function (el) {
      el.setAttribute("style", el.getAttribute("data-style-" + theme));
    });
  }

  var params = new URLSearchParams(window.location.search);
  var theme = params.get("theme") || "light";
  document.body.setAttribute("data-active-theme", theme);
  applyThemeStyles(theme);

  var toggle = document.getElementById("menu-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var expanded = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!expanded));
      console.log("menu-toggle clicked, expanded=" + !expanded);
    });
  }

  document.querySelectorAll("[data-dynamic-interval-ms]").forEach(function (el) {
    var interval = parseInt(el.getAttribute("data-dynamic-interval-ms"), 10);
    if (interval > 0) {
      setInterval(function () {
        el.textContent = "ts:" + Date.now();
      }, interval);
    }
  });
})();
