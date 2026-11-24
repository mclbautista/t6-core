/**
 * Concerns sheet (spec #732, ticket #737). Bound to the sheet; pushed with clasp.
 * It holds no rules: t6 decides which actions each row allows (the `allowed actions` column) and what the result was (`last result`).
 *
 * Script Properties (Project Settings): EXPORT_URL, READ_TOKEN, WEBHOOK_URL, SUBMIT_SECRET.
 * Nothing here is a secret, a URL or a sheet id.
 */

var SHEET_NAME = 'Concerns';
var KEEP = 'Keep';
var ID_COLUMN = 'Concern id';
var REVISION_COLUMN = 'revision';
var ALLOWED_COLUMN = 'allowed actions';
var RESULT_COLUMN = 'last result';
var ACTION_COLUMN = 'Action';
var NOTE_COLUMN = 'Note';
var SENT = 'Sent';
var REFRESH_AFTER_SUBMIT_MS = 60 * 1000;

function onOpen() {
  // A simple trigger may not call UrlFetchApp, so this only adds the menu. Refresh on open is the installable trigger from installOpenTrigger().
  SpreadsheetApp.getUi().createMenu('Concerns')
    .addItem('Refresh', 'refresh')
    .addItem('Submit', 'submit')
    .addSeparator()
    .addItem('Set up refresh on open (once)', 'installOpenTrigger')
    .addToUi();
}

/** Run once by the owner: an installable on-open trigger, so the sheet refreshes when opened. It is not a timed trigger. */
function installOpenTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'refreshOnOpen') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('refreshOnOpen').forSpreadsheet(SpreadsheetApp.getActive()).onOpen().create();
}

function refreshOnOpen() {
  try { refresh(); } catch (e) { console.error(e); }
}

/** The one-off trigger made by submit(): refresh once, then delete itself. */
function refreshAfterSubmit() {
  deleteTriggers_('refreshAfterSubmit');
  try { refresh(); } catch (e) { console.error(e); }
}

function property_(name) {
  var value = PropertiesService.getScriptProperties().getProperty(name);
  if (!value) throw new Error('Script Property ' + name + ' is not set');
  return value;
}

function deleteTriggers_(handler) {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === handler) ScriptApp.deleteTrigger(t);
  });
}

function sheet_() {
  var book = SpreadsheetApp.getActive();
  return book.getSheetByName(SHEET_NAME) || book.insertSheet(SHEET_NAME);
}

function alert_(message) {
  try { SpreadsheetApp.getUi().alert(message); } catch (e) { console.error(message); }
}

function fetchExport_() {
  var response = UrlFetchApp.fetch(property_('EXPORT_URL'), {
    method: 'get',
    headers: { 'X-T6-Read-Token': property_('READ_TOKEN') },
    muteHttpExceptions: true
  });
  if (response.getResponseCode() !== 200) throw new Error('Export answered ' + response.getResponseCode());
  return Utilities.parseCsv(response.getContentText('UTF-8'));
}

function refresh() {
  var lock = LockService.getDocumentLock();
  lock.waitLock(30000);
  try {
    var table;
    try { table = fetchExport_(); } catch (e) { alert_('Refresh failed, the sheet is unchanged: ' + e.message); throw e; }
    writeTable_(sheet_(), table);
  } finally {
    lock.releaseLock();
  }
}

function indexOf_(header, name) {
  var i = header.indexOf(name);
  if (i < 0) throw new Error('The export has no ' + name + ' column');
  return i;
}

/** Unsent picks by Concern id: {id: {action, note}} for rows whose Action is not Keep or whose Note is filled. */
function unsentPicks_(sheet) {
  var picks = {};
  var last = sheet.getLastRow();
  if (last < 2) return picks;
  var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  var id = header.indexOf(ID_COLUMN), action = header.indexOf(ACTION_COLUMN), note = header.indexOf(NOTE_COLUMN);
  if (id < 0 || action < 0 || note < 0) return picks;
  sheet.getRange(2, 1, last - 1, header.length).getValues().forEach(function (row) {
    var a = String(row[action] || KEEP), n = String(row[note] || '');
    if (row[id] !== '' && (a !== KEEP || n !== '')) picks[String(row[id])] = { action: a, note: n };
  });
  return picks;
}

function writeTable_(sheet, table) {
  var header = table[0], data = table.slice(1).filter(function (r) { return r.length === header.length; });
  var id = indexOf_(header, ID_COLUMN), allowedAt = indexOf_(header, ALLOWED_COLUMN);
  var picks = unsentPicks_(sheet);  // read before anything is cleared; an id missing from the export is dropped here
  var columns = header.concat([ACTION_COLUMN, NOTE_COLUMN]);
  var width = columns.length;

  sheet.clear();
  sheet.getRange(1, 1, 1, width).setNumberFormat('@').setValues([columns]).setFontWeight('bold');
  sheet.setFrozenRows(1);
  if (data.length > 0) {
    var rows = [], validations = [];
    data.forEach(function (row) {
      var allowed = row[allowedAt].split(', ').filter(String);
      if (allowed.indexOf(KEEP) < 0) allowed.unshift(KEEP);
      var pick = picks[row[id]] || { action: KEEP, note: '' };
      var action = allowed.indexOf(pick.action) >= 0 ? pick.action : KEEP;  // a pick t6 no longer allows falls back to Keep
      rows.push(row.concat([action, pick.note]));
      validations.push(SpreadsheetApp.newDataValidation().requireValueInList(allowed, true).setAllowInvalid(false).build());
    });
    // Plain text everywhere, so a Concern starting with = + - @ is never evaluated.
    sheet.getRange(2, 1, rows.length, width).setNumberFormat('@').setValues(rows);
    sheet.getRange(2, width - 1, rows.length, 1).setDataValidations(validations.map(function (v) { return [v]; }));
  }
  sheet.showColumns(1, width);
  [ID_COLUMN, REVISION_COLUMN, ALLOWED_COLUMN].forEach(function (name) { sheet.hideColumns(columns.indexOf(name) + 1); });
}

function submit() {
  var lock = LockService.getDocumentLock();
  lock.waitLock(30000);
  try {
    var email = Session.getActiveUser().getEmail();
    if (!email) { alert_('Google did not give the script your email, so nothing was sent.'); return; }
    var sheet = sheet_(), last = sheet.getLastRow();
    if (last < 2) { alert_('Nothing to send.'); return; }
    var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    var at = {};
    [ID_COLUMN, REVISION_COLUMN, RESULT_COLUMN, ACTION_COLUMN, NOTE_COLUMN].forEach(function (name) { at[name] = indexOf_(header, name); });
    var values = sheet.getRange(2, 1, last - 1, header.length).getValues();
    var rows = [], sheetRows = [];
    values.forEach(function (row, i) {
      var action = String(row[at[ACTION_COLUMN]] || KEEP), note = String(row[at[NOTE_COLUMN]] || '');
      if (action === KEEP && note === '') return;
      rows.push({ concern: String(row[at[ID_COLUMN]]), revision: Number(row[at[REVISION_COLUMN]]), action: action, note: note });
      sheetRows.push(i + 2);
    });
    if (rows.length === 0) { alert_('No row is changed: set an Action or type a Note first.'); return; }

    var body = JSON.stringify({ id: Utilities.getUuid(), email: email, rows: rows });
    var timestamp = String(Math.floor(Date.now() / 1000));
    var response = UrlFetchApp.fetch(property_('WEBHOOK_URL'), {
      method: 'post',
      contentType: 'application/json',
      payload: body,
      headers: { 'X-T6-Sheet-Timestamp': timestamp, 'X-T6-Sheet-Signature': sign_(property_('SUBMIT_SECRET'), timestamp, body) },
      muteHttpExceptions: true
    });
    if (response.getResponseCode() !== 200) {
      alert_('Submit was refused (' + response.getResponseCode() + '). Nothing was marked Sent.');
      return;
    }
    sheetRows.forEach(function (r) {
      sheet.getRange(r, at[RESULT_COLUMN] + 1).setValue(SENT);
      sheet.getRange(r, at[ACTION_COLUMN] + 1).setValue(KEEP);
      sheet.getRange(r, at[NOTE_COLUMN] + 1).setValue('');
    });
    deleteTriggers_('refreshAfterSubmit');
    ScriptApp.newTrigger('refreshAfterSubmit').timeBased().after(REFRESH_AFTER_SUBMIT_MS).create();  // one-off; it deletes itself
  } finally {
    lock.releaseLock();
  }
}

/** Lower-case hex HMAC-SHA256 of `{timestamp}:{body}`, keyed with the secret; both are UTF-8, matching t6's sheet_signature(). */
function sign_(secret, timestamp, body) {
  var bytes = Utilities.computeHmacSha256Signature(timestamp + ':' + body, secret);
  return bytes.map(function (b) { return ('0' + ((b + 256) % 256).toString(16)).slice(-2); }).join('');
}

/** Hand check (see README): run it from the editor and read the log. Uses the README's example secret, never the real one. */
function checkSignature() {
  console.log(sign_('example-submit-secret', '1764000000', '{"id":"c1","email":"a@b.co","rows":[]}'));
}
