import * as vscode from 'vscode';
import { getLang, isRtl, t } from './i18n';
import { RunnerResult } from './runner';

export class ReportPanel {
  private static current: ReportPanel | undefined;
  private readonly panel: vscode.WebviewPanel;
  private disposables: vscode.Disposable[] = [];
  private rerun: (() => void) | undefined;

  static show(result: RunnerResult, rerun: () => void) {
    if (ReportPanel.current) {
      ReportPanel.current.update(result, rerun);
      ReportPanel.current.panel.reveal(vscode.ViewColumn.Beside, true);
      return;
    }
    const panel = vscode.window.createWebviewPanel('odooLabReport', t('Odoo Lab report'), { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true }, {
      enableScripts: true,
      retainContextWhenHidden: true,
    });
    ReportPanel.current = new ReportPanel(panel);
    ReportPanel.current.update(result, rerun);
  }

  private constructor(panel: vscode.WebviewPanel) {
    this.panel = panel;
    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
    this.panel.webview.onDidReceiveMessage(async (msg) => {
      if (msg.type === 'reveal' && msg.file && msg.line) {
        try {
          const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(msg.file));
          const editor = await vscode.window.showTextDocument(doc, { viewColumn: vscode.ViewColumn.One, preserveFocus: false });
          const pos = new vscode.Position(Math.max(0, msg.line - 1), 0);
          editor.selection = new vscode.Selection(pos, pos);
          editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
        } catch {
          vscode.window.showWarningMessage(t('Could not open file: {0}', msg.file));
        }
      } else if (msg.type === 'rerun' && this.rerun) {
        this.rerun();
      } else if (msg.type === 'copy' && msg.text) {
        await vscode.env.clipboard.writeText(msg.text);
        vscode.window.setStatusBarMessage(t('Copied'), 2000);
      }
    }, null, this.disposables);
  }

  private update(result: RunnerResult, rerun: () => void) {
    this.rerun = rerun;
    this.panel.title = result.method ? t('Report: {0}', result.method) : t('Odoo Lab report');
    this.panel.webview.html = this.html(result);
  }

  private dispose() {
    ReportPanel.current = undefined;
    this.disposables.forEach((d) => d.dispose());
  }

  private html(result: RunnerResult): string {
    const nonce = Array.from({ length: 24 }, () => Math.floor(Math.random() * 36).toString(36)).join('');
    const data = JSON.stringify(result).replace(/</g, '\\u003c');
    const strings: Record<string, string> = {
      fail: t('Error'), warn: t('Warning'), expected: t('Intended rejection'), notreached: t('Not reached'),
      skipped: t('Skipped'), pass: t('Passed'),
      kind_records: t('Record method (button / action)'), kind_compute: t('Compute method'), kind_onchange: t('Onchange method'),
      kind_constrains: t('Constraint (constrains)'), kind_create: t('create method'), kind_write: t('write method'), kind_model: t('Model-level method'),
      cannotRun: t('Could not run the test'), unknownError: t('Unknown error'), retry: t('Try again'), rerun: t('Run again'),
      inputs: t('Inputs'), returned: t('Returned value: '), whereMethod: t('Error location in your method: '), whereFile: t('Error location in your file: '),
      line: t('Line {0}', '{0}'), why: t('Why did it happen?'), fix: t('How to fix it?'), locals: t('Variable values at the moment of the error'),
      raisedFrom: t('Raised from: '), fullTraceback: t('Full traceback'), copy: t('Copy'),
      odoo: t('Odoo {0}', '{0}'), database: t('database {0}', '{0}'), user: t('user {0}', '{0}'), seconds: t('{0} s', '{0}'),
      covered: t('Method lines executed: {0} of {1}', '{0}', '{1}'), missing: t('Lines not reached by any scenario: '),
      noScenarios: t('No scenarios were generated for this method.'), details: t('Details'), sep: getLang() === 'ar' ? '، ' : ', ',
    };
    const lang = getLang();
    const dir = isRtl() ? 'rtl' : 'ltr';
    return `<!DOCTYPE html>
<html lang="${lang}" dir="${dir}">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style nonce="${nonce}">
  :root { --fail: var(--vscode-testing-iconFailed, #f14c4c); --pass: var(--vscode-testing-iconPassed, #73c991);
          --warn: var(--vscode-editorWarning-foreground, #cca700); --muted: var(--vscode-descriptionForeground);
          --line: var(--vscode-panel-border, rgba(128,128,128,.35)); --card: var(--vscode-editorWidget-background); }
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground);
         background: var(--vscode-editor-background); padding: 16px 20px 40px; line-height: 1.65; }
  h1 { font-size: 1.35em; margin: 0 0 2px; font-weight: 600; }
  code, .mono { font-family: var(--vscode-editor-font-family); font-size: .95em; direction: ltr; unicode-bidi: isolate; }
  .sub { color: var(--muted); margin: 0 0 14px; }
  .top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
  button { font: inherit; border: 0; border-radius: 3px; padding: 5px 12px; cursor: pointer;
           background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
  button:hover { background: var(--vscode-button-hoverBackground); }
  button.link { background: none; color: var(--vscode-textLink-foreground); padding: 0; text-align: start; }
  button.link:hover { text-decoration: underline; background: none; }
  .summary { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 16px; }
  .chip { border: 1px solid var(--line); border-radius: 12px; padding: 2px 10px; font-size: .92em; }
  .chip b { margin-inline-end: 4px; }
  .s-fail { color: var(--fail); } .s-pass { color: var(--pass); } .s-warn, .s-expected { color: var(--warn); }
  .s-skipped, .s-notreached { color: var(--muted); }
  .banner { border-inline-start: 3px solid var(--warn); background: var(--card); padding: 6px 10px; margin: 6px 0; }
  .coverage { margin: 0 0 16px; color: var(--muted); }
  .bar { height: 5px; background: var(--line); border-radius: 3px; overflow: hidden; margin-top: 4px; max-width: 360px; }
  .bar i { display: block; height: 100%; background: var(--pass); }
  details { border: 1px solid var(--line); border-radius: 4px; margin-bottom: 8px; background: var(--card); }
  details[open] { padding-bottom: 8px; }
  summary { cursor: pointer; padding: 8px 12px; display: flex; gap: 10px; align-items: baseline; list-style: none; }
  summary::-webkit-details-marker { display: none; }
  summary .badge { font-size: .8em; font-weight: 600; min-width: 64px; }
  summary .title { font-weight: 600; flex: 1; }
  summary .etype { color: var(--muted); font-size: .9em; }
  .body { padding: 0 12px; }
  .desc { color: var(--muted); margin: 0 0 8px; }
  table { border-collapse: collapse; margin: 4px 0 10px; width: 100%; }
  td { border-top: 1px solid var(--line); padding: 3px 8px; vertical-align: top; }
  td:first-child { color: var(--muted); white-space: nowrap; width: 1%; }
  .where { background: var(--vscode-inputValidation-errorBackground, rgba(241,76,76,.1)); border: 1px solid var(--vscode-inputValidation-errorBorder, var(--fail));
           border-radius: 3px; padding: 6px 10px; margin: 6px 0 10px; }
  .where pre { margin: 4px 0 0; white-space: pre-wrap; }
  h3 { font-size: 1em; margin: 12px 0 4px; }
  .fix { border-inline-start: 3px solid var(--pass); padding: 2px 10px; }
  pre.tb { max-height: 260px; overflow: auto; background: var(--vscode-textCodeBlock-background); padding: 8px; direction: ltr; text-align: left; font-size: .85em; }
  .err-box { border: 1px solid var(--fail); padding: 12px; border-radius: 4px; }
  .muted { color: var(--muted); }
</style>
</head>
<body>
<div id="app"></div>
<script nonce="${nonce}">
const vscode = acquireVsCodeApi();
const R = ${data};
const S = ${JSON.stringify(strings)};
const fmt = (tpl, ...a) => tpl.replace(/\\{(\\d+)\\}/g, (m, i) => a[+i] !== undefined ? a[+i] : m);
const STATUS = {
  fail: [S.fail, 's-fail'], warn: [S.warn, 's-warn'], expected: [S.expected, 's-expected'],
  notreached: [S.notreached, 's-notreached'], skipped: [S.skipped, 's-skipped'], pass: [S.pass, 's-pass']
};
const ORDER = ['fail', 'warn', 'expected', 'notreached', 'skipped', 'pass'];
const KIND = { records: S.kind_records, compute: S.kind_compute, onchange: S.kind_onchange,
  constrains: S.kind_constrains, create: S.kind_create, write: S.kind_write, model: S.kind_model };

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v; else if (k === 'style') el.style.cssText = v; else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) el.setAttribute(k, v);
  }
  for (const c of children.flat()) { if (c === null || c === undefined || c === false) continue; el.append(c.nodeType ? c : document.createTextNode(String(c))); }
  return el;
}
const reveal = (file, line) => vscode.postMessage({ type: 'reveal', file, line });
const kv = (obj) => h('table', {}, Object.entries(obj || {}).map(([k, v]) => h('tr', {}, h('td', {}, k), h('td', {}, h('code', {}, v)))));

function renderError() {
  const e = R.error || {};
  return h('div', { class: 'err-box' },
    h('h1', {}, S.cannotRun),
    h('p', {}, e.message || S.unknownError),
    e.detail ? h('pre', { class: 'tb' }, e.detail) : null,
    h('button', { onclick: () => vscode.postMessage({ type: 'rerun' }) }, S.retry));
}

function renderScenario(s) {
  const [label, cls] = STATUS[s.status] || [s.status, ''];
  const e = s.error;
  const open = s.status === 'fail' || s.status === 'warn';
  const body = h('div', { class: 'body' },
    h('p', { class: 'desc' }, s.description),
    h('h3', {}, S.inputs), kv(s.inputs),
    s.skipReason ? h('p', { class: 'muted' }, s.skipReason) : null,
    s.returned && !e ? h('p', { class: 'muted' }, S.returned, h('code', {}, s.returned)) : null);

  if (e) {
    if (e.userLine) {
      body.append(h('div', { class: 'where' },
        h('div', {}, e.inUserMethod ? S.whereMethod : S.whereFile,
          h('button', { class: 'link', onclick: () => reveal(R.file, e.userLine) }, fmt(S.line, e.userLine))),
        e.userCode ? h('pre', { class: 'mono' }, e.userCode) : null));
    } else if (e.note) {
      body.append(h('div', { class: 'where' }, e.note));
    }
    body.append(h('h3', {}, S.why), h('p', {}, e.explanation));
    body.append(h('h3', {}, S.fix), h('p', { class: 'fix' }, e.fix));
    if (e.locals && Object.keys(e.locals).length) {
      body.append(h('h3', {}, S.locals), kv(e.locals));
    }
    if (e.raisedAt) {
      body.append(h('p', { class: 'muted' }, S.raisedFrom,
        h('button', { class: 'link', onclick: () => reveal(e.raisedAt.file, e.raisedAt.line) },
          e.raisedAt.function + ' (' + e.raisedAt.file.split(/[\\\\/]/).slice(-2).join('/') + ':' + e.raisedAt.line + ')')));
    }
    body.append(h('details', {}, h('summary', {}, S.fullTraceback),
      h('pre', { class: 'tb' }, e.traceback),
      h('button', { onclick: () => vscode.postMessage({ type: 'copy', text: e.traceback }) }, S.copy)));
  }
  return h('details', open ? { open: '' } : {},
    h('summary', {}, h('span', { class: 'badge ' + cls }, label), h('span', { class: 'title' }, s.title),
      e ? h('span', { class: 'etype mono' }, e.type + (e.userLine ? ' · L' + e.userLine : '')) : null),
    body);
}

function render() {
  const app = document.getElementById('app');
  if (!R.ok) { app.append(renderError()); return; }
  const scenarios = [...(R.scenarios || [])].sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status));
  const counts = {};
  scenarios.forEach((s) => { counts[s.status] = (counts[s.status] || 0) + 1; });

  app.append(h('div', { class: 'top' },
    h('div', {}, h('h1', {}, h('span', { class: 'mono' }, R.model + '.' + R.method)),
      h('p', { class: 'sub' }, [KIND[R.kind] || R.kind, fmt(S.odoo, R.odooVersion), fmt(S.database, R.database), fmt(S.user, R.runAs), fmt(S.seconds, (R.totalMs / 1000).toFixed(1))].join(' · '))),
    h('button', { onclick: () => vscode.postMessage({ type: 'rerun' }) }, S.rerun)));

  app.append(h('div', { class: 'summary' }, ORDER.filter((k) => counts[k]).map((k) =>
    h('span', { class: 'chip ' + STATUS[k][1] }, h('b', {}, counts[k]), STATUS[k][0]))));

  const exec = R.execLines || [];
  const covered = (R.coveredLines || []).filter((l) => exec.includes(l));
  if (exec.length) {
    const pct = Math.round((covered.length / exec.length) * 100);
    const missing = exec.filter((l) => !covered.includes(l));
    const cov = h('div', { class: 'coverage' }, fmt(S.covered, covered.length, exec.length),
      h('div', { class: 'bar' }, h('i', { style: 'width:' + pct + '%' })));
    if (missing.length) {
      cov.append(h('div', {}, S.missing,
        missing.slice(0, 15).map((l, i) => [i ? S.sep : '', h('button', { class: 'link', onclick: () => reveal(R.file, l) }, String(l))])));
    }
    app.append(cov);
  }
  [...(R.warnings || []), ...(R.notes || [])].forEach((w) => app.append(h('div', { class: 'banner' }, w)));
  if (!scenarios.length) app.append(h('p', { class: 'muted' }, S.noScenarios));
  scenarios.forEach((s) => app.append(renderScenario(s)));
}
render();
</script>
</body>
</html>`;
  }
}
