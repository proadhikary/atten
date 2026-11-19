// Mobile nav toggle
(function () {
  var t = document.getElementById('navToggle');
  var links = document.getElementById('navLinks');
  if (t && links) {
    t.addEventListener('click', function () { links.classList.toggle('open'); });
  }
  // Auto-dismiss flash messages after 6s
  setTimeout(function () {
    document.querySelectorAll('.flash-wrap .flash').forEach(function (f) {
      f.style.transition = 'opacity .4s';
      f.style.opacity = '0';
      setTimeout(function () { f.remove(); }, 400);
    });
  }, 6000);
})();
