// Minimal behaviour: select-all checkboxes, selected count, details row toggle.
(function () {
  function updateCount(root) {
    var form = (root || document).querySelector("#queue-form");
    if (!form) return;
    var n = form.querySelectorAll(".album-check:checked").length;
    form.querySelectorAll("[data-selected-count]").forEach(function (el) { el.textContent = n; });
  }

  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t.matches("[data-select-all]")) {
      document.querySelectorAll(t.getAttribute("data-select-all")).forEach(function (cb) { cb.checked = t.checked; });
    }
    if (t.matches(".album-check, [data-select-all]")) updateCount();
  });

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-toggle-row]");
    if (!btn) return;
    var row = document.querySelector(btn.getAttribute("data-toggle-row"));
    if (!row) return;
    var opening = row.classList.contains("d-none");
    row.classList.toggle("d-none");
    btn.textContent = opening ? "Hide" : "Details";
    if (!opening) e.preventDefault();  // closing: don't refetch
  }, true);

  document.addEventListener("htmx:afterSwap", function () { updateCount(); });

  // Don't fire the details request when the click was closing the row.
  document.addEventListener("htmx:confirm", function (e) {
    var el = e.detail.elt;
    if (el && el.matches("[data-toggle-row]")) {
      var row = document.querySelector(el.getAttribute("data-toggle-row"));
      if (row && row.classList.contains("d-none")) e.preventDefault();
    }
  });
})();
