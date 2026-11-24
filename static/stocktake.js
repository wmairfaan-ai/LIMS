// Live stocktake scanning: each scan is saved in the background, without reloading the page.
// Without JavaScript the same form still works, it just reloads after every scan.
(function () {
  'use strict';

  var form = document.getElementById('scan-form');
  var finishForm = document.getElementById('finish-form');
  if (!form || !finishForm || !window.fetch) return;

  var input = form.elements.code;
  var token = form.elements.csrf_token.value;
  var log = document.getElementById('scan-log');
  var rows = document.getElementById('stocktake-rows');
  var nFound = document.getElementById('n-found');
  var nExpected = document.getElementById('n-expected');

  var LOG_LENGTH = 8;
  var TIMEOUT_MS = 15000;
  var queue = [];        // scans waiting to be saved, in the order they were scanned
  var saving = false;
  var failed = 0;        // scans that could not be saved and still need Retry

  function setLine(line, level, message) {
    line.className = 'log-' + level;
    line.textContent = '';
    var text = document.createElement('span');
    text.textContent = message;
    line.appendChild(text);
  }

  function addLine(message) {
    var line = document.createElement('li');
    setLine(line, 'wait', message);
    log.insertBefore(line, log.firstChild);
    // Forget old lines, but never one that still needs attention
    for (var i = log.children.length - 1; i >= LOG_LENGTH; i--) {
      if (/log-(ok|warn)/.test(log.children[i].className)) log.removeChild(log.children[i]);
    }
    return line;
  }

  function notSaved(scan, message) {
    failed += 1;
    setLine(scan.line, 'danger', scan.name + ' was NOT saved. ' + message);
    var retry = document.createElement('button');
    retry.type = 'button';
    retry.textContent = 'Retry';
    retry.addEventListener('click', function () {
      failed -= 1;
      setLine(scan.line, 'wait', scan.name + ': saving…');
      queue.push(scan);
      saveNext();
      input.focus();
    });
    scan.line.appendChild(retry);
  }

  function send(code) {
    var body = new FormData();
    body.append('csrf_token', token);
    body.append('code', code);
    var stop = window.AbortController ? new AbortController() : null;
    var timer = stop && setTimeout(function () { stop.abort(); }, TIMEOUT_MS);
    return fetch(form.dataset.api, { method: 'POST', body: body, credentials: 'same-origin', signal: stop ? stop.signal : undefined })
      .then(function (res) { return res.json().then(function (data) { return { status: res.status, data: data }; }); })
      .finally(function () { clearTimeout(timer); });
  }

  function saveNext() {
    if (saving || !queue.length) return;
    saving = true;
    var scan = queue.shift();
    send(scan.code).then(function (reply) {
      var data = reply.data;
      if (reply.status !== 200) {
        notSaved(scan, data.message);
        if (data.finished) input.disabled = true;
        return;
      }
      setLine(scan.line, data.level, data.message);
      rows.innerHTML = data.rows;   // built by the server template, which escapes every value
      nFound.textContent = data.found;
      nExpected.textContent = data.expected;
    }).catch(function () {
      notSaved(scan, 'ChemTrack could not be reached.');
    }).then(function () {
      saving = false;
      saveNext();
    });
  }

  input.focus();   // scanners type into whatever has the cursor

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var code = input.value.trim();
    input.value = '';            // ready for the next scan straight away
    input.focus();
    if (!code) return;
    var name = code.split('/c/').pop();   // QR labels hold a web address ending in /c/CT-00042
    queue.push({ code: code, name: name, line: addLine(name + ': saving…') });
    saveNext();
  });

  // Finishing marks everything unscanned as missing, so never finish over an unsaved scan
  finishForm.addEventListener('submit', function (e) {
    var missing = Number(nExpected.textContent) - Number(nFound.textContent);
    if (saving || queue.length) {
      e.preventDefault();
      alert('Some scans are still being saved. Try again in a moment.');
    } else if (failed) {
      e.preventDefault();
      alert(failed + ' scan(s) were not saved. Press Retry on each one first, or reload the page to discard them.');
    } else if (!confirm('Finish? ' + missing + ' container(s) not scanned will be marked missing.')) {
      e.preventDefault();
    }
  });

  window.addEventListener('beforeunload', function (e) {
    if (saving || queue.length || failed) { e.preventDefault(); e.returnValue = ''; }
  });
}());
