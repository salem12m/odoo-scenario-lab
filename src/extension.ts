import * as vscode from 'vscode';
import { missingSettings, pythonSourceLabel, readConfig, runSetupWizard } from './config';
import { clearI18nCache, initI18n, t } from './i18n';
import { ReportPanel } from './report';
import { RunnerResult, runRunner } from './runner';

let output: vscode.OutputChannel;
let diagnostics: vscode.DiagnosticCollection;
let failDecoration: vscode.TextEditorDecorationType;
let running = false;

const MODEL_HINT = /(models\.(Model|TransientModel|AbstractModel)|_inherit\s*=|_name\s*=)/;

class OdooCodeLensProvider implements vscode.CodeLensProvider {
  private readonly emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this.emitter.event;

  refresh() {
    this.emitter.fire();
  }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    if (!vscode.workspace.getConfiguration('odooLab', document.uri).get<boolean>('enableCodeLens', true)) {
      return [];
    }
    if (!MODEL_HINT.test(document.getText())) {
      return [];
    }
    const lenses: vscode.CodeLens[] = [];
    const defRe = /^([ \t]+)def\s+\w+\s*\(\s*self\b/;
    const title = '▶ ' + t('Test scenarios');
    for (let i = 0; i < document.lineCount; i++) {
      const line = document.lineAt(i).text;
      if (defRe.test(line)) {
        lenses.push(new vscode.CodeLens(new vscode.Range(i, 0, i, line.length), {
          title,
          command: 'odooLab.runScenarios',
          arguments: [document.uri, i + 1],
        }));
      }
    }
    return lenses;
  }
}

function applyResultToEditor(uri: vscode.Uri, result: RunnerResult) {
  diagnostics.delete(uri);
  const editor = vscode.window.visibleTextEditors.find((e) => e.document.uri.toString() === uri.toString());
  if (!result.ok || !Array.isArray(result.scenarios)) {
    editor?.setDecorations(failDecoration, []);
    return;
  }
  const byLine = new Map<number, { severity: vscode.DiagnosticSeverity; messages: string[] }>();
  for (const s of result.scenarios) {
    const e = s.error;
    if (!e?.userLine || !(s.status === 'fail' || s.status === 'warn')) {
      continue;
    }
    const severity = s.status === 'fail' ? vscode.DiagnosticSeverity.Error : vscode.DiagnosticSeverity.Warning;
    const entry = byLine.get(e.userLine) ?? { severity, messages: [] };
    if (severity === vscode.DiagnosticSeverity.Error) {
      entry.severity = severity;
    }
    entry.messages.push(`[${s.title}] ${e.type}: ${e.category}`);
    byLine.set(e.userLine, entry);
  }
  const doc = vscode.workspace.textDocuments.find((d) => d.uri.toString() === uri.toString());
  const diags: vscode.Diagnostic[] = [];
  const ranges: vscode.Range[] = [];
  for (const [line, entry] of byLine) {
    const idx = line - 1;
    const text = doc && idx < doc.lineCount ? doc.lineAt(idx) : undefined;
    const range = text
      ? new vscode.Range(idx, text.firstNonWhitespaceCharacterIndex, idx, text.text.length)
      : new vscode.Range(idx, 0, idx, 200);
    const d = new vscode.Diagnostic(range, entry.messages.join('\n'), entry.severity);
    d.source = 'Odoo Lab';
    diags.push(d);
    if (entry.severity === vscode.DiagnosticSeverity.Error) {
      ranges.push(range);
    }
  }
  diagnostics.set(uri, diags);
  editor?.setDecorations(failDecoration, ranges);
}

async function ensureConfigured(uri: vscode.Uri): Promise<boolean> {
  const missing = missingSettings(await readConfig(uri));
  if (!missing.length) {
    return true;
  }
  const start = t('Start setup');
  const choice = await vscode.window.showWarningMessage(
    t('The connection to Odoo must be configured first. Missing: {0}', missing.join(', ')),
    start,
  );
  if (choice === start) {
    return runSetupWizard();
  }
  return false;
}

async function runScenarios(context: vscode.ExtensionContext, uriArg?: vscode.Uri, lineArg?: number) {
  const editor = vscode.window.activeTextEditor;
  const uri = uriArg instanceof vscode.Uri ? uriArg : editor?.document.uri;
  if (!uri || uri.scheme !== 'file') {
    vscode.window.showWarningMessage(t('Open a Python file from an Odoo module and place the cursor inside a method.'));
    return;
  }
  const doc = await vscode.workspace.openTextDocument(uri);
  if (doc.languageId !== 'python') {
    vscode.window.showWarningMessage(t('This extension works on Python files only.'));
    return;
  }
  const line = typeof lineArg === 'number'
    ? lineArg
    : editor && editor.document.uri.toString() === uri.toString()
      ? editor.selection.active.line + 1
      : 1;

  if (running) {
    vscode.window.showInformationMessage(t('A test is already running.'));
    return;
  }
  if (!(await ensureConfigured(uri))) {
    return;
  }
  if (doc.isDirty) {
    await doc.save();
  }

  const config = await readConfig(uri);
  output.appendLine(t('Python used ({0}): {1}', pythonSourceLabel(config.pythonSource), config.pythonPath));
  const rerun = () => runScenarios(context, uri, line);
  running = true;
  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: t('Odoo Lab: Running scenarios…'), cancellable: true },
      (_progress, token) => runRunner(context, config, ['--file', uri.fsPath, '--line', String(line)], output, token),
    );
    if (result.error?.code === 'cancelled') {
      return;
    }
    applyResultToEditor(uri, result);
    ReportPanel.show(result, rerun);

    if (!result.ok) {
      const openSettings = t('Open settings');
      const showLog = t('Show log');
      const choice = await vscode.window.showErrorMessage(result.error?.message ?? t('Could not run the test.'), openSettings, showLog);
      if (choice === openSettings) {
        vscode.commands.executeCommand('workbench.action.openSettings', 'odooLab');
      } else if (choice === showLog) {
        output.show();
      }
      return;
    }
    const fails = (result.scenarios as any[]).filter((s) => s.status === 'fail').length;
    const total = (result.scenarios as any[]).length;
    vscode.window.setStatusBarMessage(
      fails
        ? t('Odoo Lab: {0} failing of {1} scenarios', fails, total)
        : t('Odoo Lab: all {0} scenarios passed without errors', total),
      8000,
    );
  } finally {
    running = false;
  }
}

async function checkConnection(context: vscode.ExtensionContext) {
  const uri = vscode.window.activeTextEditor?.document.uri ?? vscode.workspace.workspaceFolders?.[0]?.uri ?? vscode.Uri.file('.');
  if (!(await ensureConfigured(uri))) {
    return;
  }
  const config = await readConfig(uri);
  const result = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: t('Odoo Lab: Checking connection…'), cancellable: true },
    (_p, token) => runRunner(context, config, ['--check'], output, token),
  );
  if (result.ok) {
    vscode.window.showInformationMessage(
      t('Connected successfully ✓ Odoo {0} · database {1} · {2} installed modules · Python {3} ({4}: {5})',
        result.odooVersion, result.database, result.installedModules, result.python,
        pythonSourceLabel(config.pythonSource), config.pythonPath),
    );
  } else if (result.error?.code !== 'cancelled') {
    const edit = t('Edit setup');
    const showLog = t('Show log');
    const choice = await vscode.window.showErrorMessage(result.error?.message ?? t('Connection failed.'), edit, showLog);
    if (choice === edit) {
      vscode.commands.executeCommand('odooLab.configure');
    } else if (choice === showLog) {
      if (result.error?.detail) {
        output.appendLine(result.error.detail);
      }
      output.show();
    }
  }
}

export function activate(context: vscode.ExtensionContext) {
  initI18n(context);
  output = vscode.window.createOutputChannel('Odoo Scenario Lab');
  diagnostics = vscode.languages.createDiagnosticCollection('odooLab');
  failDecoration = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: new vscode.ThemeColor('inputValidation.errorBackground'),
    overviewRulerColor: new vscode.ThemeColor('editorError.foreground'),
    overviewRulerLane: vscode.OverviewRulerLane.Right,
  });
  const lens = new OdooCodeLensProvider();

  context.subscriptions.push(
    output,
    diagnostics,
    failDecoration,
    vscode.languages.registerCodeLensProvider({ language: 'python', scheme: 'file' }, lens),
    vscode.commands.registerCommand('odooLab.runScenarios', (uri?: vscode.Uri, line?: number) => runScenarios(context, uri, line)),
    vscode.commands.registerCommand('odooLab.configure', async () => {
      if (await runSetupWizard()) {
        const checkNow = t('Check connection now');
        const choice = await vscode.window.showInformationMessage(t('Setup saved.'), checkNow);
        if (choice === checkNow) {
          checkConnection(context);
        }
      }
    }),
    vscode.commands.registerCommand('odooLab.checkConnection', () => checkConnection(context)),
    vscode.commands.registerCommand('odooLab.clearResults', () => {
      diagnostics.clear();
      vscode.window.visibleTextEditors.forEach((e) => e.setDecorations(failDecoration, []));
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('odooLab.language')) {
        clearI18nCache();
        lens.refresh();
      }
      if (e.affectsConfiguration('odooLab.enableCodeLens')) {
        lens.refresh();
      }
    }),
    vscode.workspace.onDidChangeTextDocument((e) => {
      if (diagnostics.has(e.document.uri) && e.contentChanges.length) {
        diagnostics.delete(e.document.uri);
        vscode.window.visibleTextEditors
          .filter((ed) => ed.document === e.document)
          .forEach((ed) => ed.setDecorations(failDecoration, []));
      }
    }),
  );
}

export function deactivate() {}
