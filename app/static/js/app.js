// RedSaves WebUI: after a manual "Scan now", poll /api/status and reload the
// dashboard once the background scan finishes.
(function () {
  var form = document.querySelector('form.scanbox');
  if (!form) return;
  var wasRunning = false;
  form.addEventListener('submit', function () {
    var btn = form.querySelector('button');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Scanning…'; }
    var iv = setInterval(function () {
      fetch('/api/status').then(function (r) { return r.json(); }).then(function (s) {
        if (s.scan_running) { wasRunning = true; return; }
        if (wasRunning) { clearInterval(iv); location.reload(); }
      }).catch(function () {});
    }, 3000);
  });
})();
