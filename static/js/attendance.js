(function () {
  var btnIn = document.getElementById('btnCheckIn');
  var btnOut = document.getElementById('btnCheckOut');
  var msg = document.getElementById('geoMsg');

  function setMsg(text, cls) {
    msg.textContent = text;
    msg.className = 'geo-msg ' + (cls || '');
  }

  /* Stable-ish traits of this browser. The server hashes them so a member who
     clears cookies is still recognised as the same device rather than being
     handed a fresh device slot. Nothing here identifies a person. */
  function deviceSignals() {
    var s = window.screen || {};
    return {
      ua: navigator.userAgent || '',
      platform: (navigator.userAgentData && navigator.userAgentData.platform) ||
                navigator.platform || '',
      screen: [s.width, s.height, s.colorDepth, window.devicePixelRatio].join('x'),
      tz: (Intl.DateTimeFormat().resolvedOptions().timeZone) || '',
      lang: (navigator.languages || [navigator.language]).join(','),
      cores: navigator.hardwareConcurrency || 0,
      memory: navigator.deviceMemory || 0,
      touch: navigator.maxTouchPoints || 0
    };
  }

  var LAB = window.LAB || {};
  var API = LAB.api || '/api/attendance';

  /* Post the mark. `pos` is null when we have no position — either the admin
     turned the location check off, or the browser refused while it's only on
     Warn. The server decides whether that's acceptable. */
  function send(action, btn, original, pos) {
      fetch(API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: action,
          lat: pos ? pos.coords.latitude : null,
          lng: pos ? pos.coords.longitude : null,
          accuracy: pos ? pos.coords.accuracy : null,
          device: deviceSignals()
        })
      })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        var d = res.d;
        if (res.ok && d.ok) {
          if (action === 'in') {
            document.getElementById('checkinTime').textContent = d.check_in;
            document.querySelectorAll('.status-box')[0].classList.add('done');
            btnOut.disabled = false;
            btn.textContent = '✓ Checked in';
            setMsg(d.distance != null
              ? 'Checked in — ' + Math.round(d.distance) + ' m from the lab.'
              : 'Checked in.', 'geo-ok');
          } else {
            document.getElementById('checkoutTime').textContent = d.check_out;
            document.querySelectorAll('.status-box')[1].classList.add('done');
            btn.textContent = '✓ Checked out';
            setMsg('Checked out' + (d.hours != null ? ' — ' + d.hours + ' h logged.' : '.'), 'geo-ok');
          }
        } else {
          btn.disabled = false;
          btn.textContent = original;
          setMsg(d.error || 'Something went wrong.', 'geo-err');
        }
      })
      .catch(function () {
        btn.disabled = false;
        btn.textContent = original;
        setMsg('Network error. Please try again.', 'geo-err');
      });
  }

  function mark(action, btn) {
    var original = btn.textContent;

    // Location check off: skip the position entirely. Asking would fail
    // anyway on a non-HTTPS address, which is one reason to switch it off.
    if (LAB.geofence === 'off') {
      btn.disabled = true;
      setMsg('Marking…', 'geo-loading');
      send(action, btn, original, null);
      return;
    }

    if (!navigator.geolocation) {
      if (LAB.geofence === 'warn') { btn.disabled = true; send(action, btn, original, null); return; }
      setMsg('Geolocation is not supported on this device.', 'geo-err');
      return;
    }
    btn.disabled = true;
    setMsg('📡 Getting your location…', 'geo-loading');

    navigator.geolocation.getCurrentPosition(function (pos) {
      send(action, btn, original, pos);
    }, function (err) {
      // On Warn the mark still goes through without a position; on Enforce
      // there is nothing to send.
      if (LAB.geofence === 'warn') { send(action, btn, original, null); return; }
      btn.disabled = false;
      var m = 'Could not get your location.';
      if (err.code === 1) m = 'Location permission denied. Please allow location access.';
      else if (err.code === 3) m = 'Location timed out. Try again.';
      setMsg(m, 'geo-err');
    }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 });
  }

  if (btnIn) btnIn.addEventListener('click', function () { mark('in', btnIn); });
  if (btnOut) btnOut.addEventListener('click', function () { mark('out', btnOut); });
})();
