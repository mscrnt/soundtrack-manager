// Follow the OS light/dark preference (Bootstrap 5.3 colour modes).
(function () {
  var mq = window.matchMedia("(prefers-color-scheme: dark)");
  function apply() { document.documentElement.setAttribute("data-bs-theme", mq.matches ? "dark" : "light"); }
  apply();
  if (mq.addEventListener) mq.addEventListener("change", apply);
})();
