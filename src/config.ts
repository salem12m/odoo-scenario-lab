import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';
import { t } from './i18n';

export type PythonSource = 'setting' | 'interpreter' | 'fallback';

export interface LabConfig {
  pythonPath: string;
  pythonSource: PythonSource;
  odooPath: string;
  configFile: string;
  database: string;
  runAsUser: string;
  timeoutSeconds: number;
  maxScenarios: number;
  allowProductionDatabase: boolean;
}

function expand(value: string, folder?: vscode.WorkspaceFolder): string {
  if (!value) {
    return value;
  }
  let out = value;
  const ws = folder?.uri.fsPath ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
  out = out.replace(/\$\{workspaceFolder\}/g, ws);
  out = out.replace(/\$\{userHome\}/g, os.homedir());
  if (out.startsWith('~')) {
    out = path.join(os.homedir(), out.slice(1));
  }
  return out;
}

/**
 * Returns the interpreter selected with "Python: Select Interpreter" (ms-python.python),
 * or undefined when the Python extension is not installed or nothing is selected.
 */
export async function getSelectedInterpreter(resource?: vscode.Uri): Promise<string | undefined> {
  const ext = vscode.extensions.getExtension('ms-python.python');
  if (!ext) {
    return undefined;
  }
  try {
    const api: any = ext.isActive ? ext.exports : await ext.activate();
    const envs = api?.environments;
    if (envs?.getActiveEnvironmentPath) {
      const active = envs.getActiveEnvironmentPath(resource);
      const resolved = await envs.resolveEnvironment(active);
      const exe = resolved?.executable?.uri?.fsPath;
      if (exe) {
        return exe;
      }
      if (active?.path && fs.existsSync(active.path) && fs.statSync(active.path).isFile()) {
        return active.path;
      }
    }
    const legacy = api?.settings?.getExecutionDetails?.(resource)?.execCommand?.[0];
    return legacy || undefined;
  } catch {
    return undefined;
  }
}

export async function readConfig(resource?: vscode.Uri): Promise<LabConfig> {
  const folder = resource ? vscode.workspace.getWorkspaceFolder(resource) : undefined;
  const cfg = vscode.workspace.getConfiguration('odooLab', resource);
  let python = cfg.get<string>('pythonPath', '');
  let pythonSource: PythonSource = 'setting';
  if (!python) {
    python = (await getSelectedInterpreter(resource)) ?? '';
    pythonSource = 'interpreter';
  }
  if (!python) {
    python = process.platform === 'win32' ? 'python' : 'python3';
    pythonSource = 'fallback';
  }
  return {
    pythonPath: expand(python, folder),
    pythonSource,
    odooPath: expand(cfg.get<string>('odooPath', ''), folder),
    configFile: expand(cfg.get<string>('configFile', ''), folder),
    database: cfg.get<string>('database', ''),
    runAsUser: cfg.get<string>('runAsUser', ''),
    timeoutSeconds: cfg.get<number>('timeoutSeconds', 20),
    maxScenarios: cfg.get<number>('maxScenarios', 30),
    allowProductionDatabase: cfg.get<boolean>('allowProductionDatabase', false),
  };
}

export function pythonSourceLabel(source: PythonSource): string {
  switch (source) {
    case 'setting':
      return t('from extension settings');
    case 'interpreter':
      return t('selected in VS Code');
    default:
      return t('default');
  }
}

export function missingSettings(c: LabConfig): string[] {
  const missing: string[] = [];
  if (!c.odooPath || !fs.existsSync(path.join(c.odooPath, 'odoo-bin'))) {
    missing.push(t('Odoo folder path (must contain odoo-bin)'));
  }
  if (!c.configFile || !fs.existsSync(c.configFile)) {
    missing.push(t('odoo.conf file'));
  }
  return missing;
}

function candidatePythons(odooPath: string): string[] {
  const bin = process.platform === 'win32' ? path.join('Scripts', 'python.exe') : path.join('bin', 'python');
  const roots = new Set<string>();
  for (const f of vscode.workspace.workspaceFolders ?? []) {
    roots.add(f.uri.fsPath);
  }
  if (odooPath) {
    roots.add(odooPath);
    roots.add(path.dirname(odooPath));
  }
  const found: string[] = [];
  for (const root of roots) {
    for (const venv of ['venv', '.venv', 'env', 'odoo-venv', 'venv-odoo']) {
      const p = path.join(root, venv, bin);
      if (fs.existsSync(p) && !found.includes(p)) {
        found.push(p);
      }
    }
  }
  return found;
}

export async function runSetupWizard(): Promise<boolean> {
  const target = vscode.workspace.workspaceFolders?.length
    ? vscode.ConfigurationTarget.Workspace
    : vscode.ConfigurationTarget.Global;
  const cfg = vscode.workspace.getConfiguration('odooLab');
  const current = await readConfig();
  const selected = await getSelectedInterpreter();

  // 1. Odoo folder
  const odooPick = await vscode.window.showOpenDialog({
    title: t('Step 1 of 4: Select the Odoo folder (the one containing odoo-bin)'),
    canSelectFolders: true,
    canSelectFiles: false,
    defaultUri: current.odooPath ? vscode.Uri.file(current.odooPath) : undefined,
    openLabel: t('Select this folder'),
  });
  if (!odooPick?.length) {
    return false;
  }
  const odooPath = odooPick[0].fsPath;
  if (!fs.existsSync(path.join(odooPath, 'odoo-bin'))) {
    vscode.window.showErrorMessage(t('The selected folder does not contain odoo-bin. Select the Odoo root folder.'));
    return false;
  }

  // 2. Python
  const candidates = candidatePythons(odooPath).filter((p) => p !== selected);
  const items: (vscode.QuickPickItem & { value?: string })[] = [];
  items.push({
    label: '$(check) ' + t('Use the Python selected in VS Code'),
    description: selected ?? t('not selected yet'),
    detail: t('Follows "Python: Select Interpreter" automatically (recommended)'),
    value: '__selected__',
  });
  candidates.forEach((p) => items.push({ label: p, description: t('detected environment'), value: p }));
  if (current.pythonSource === 'setting' && current.pythonPath !== selected && !candidates.includes(current.pythonPath)) {
    items.push({ label: current.pythonPath, description: t('currently saved'), value: current.pythonPath });
  }
  items.push({ label: t('Browse for a Python executable…'), value: '__browse__' });
  const pyChoice = await vscode.window.showQuickPick(items, {
    title: t('Step 2 of 4: Select the Python where Odoo requirements are installed'),
    placeHolder: t("Usually inside Odoo's virtualenv"),
  });
  if (!pyChoice) {
    return false;
  }
  let pythonPath = pyChoice.value ?? '';
  if (pythonPath === '__selected__') {
    pythonPath = '';
    if (!selected) {
      const pick = await vscode.window.showWarningMessage(
        t('No Python interpreter is selected in VS Code yet. Select one with "Python: Select Interpreter".'),
        t('Select now'),
      );
      if (pick) {
        await vscode.commands.executeCommand('python.setInterpreter');
      }
    }
  } else if (pythonPath === '__browse__') {
    const file = await vscode.window.showOpenDialog({ title: t('Select a Python executable'), canSelectFiles: true, openLabel: t('Select') });
    if (!file?.length) {
      return false;
    }
    pythonPath = file[0].fsPath;
  }

  // 3. odoo.conf
  const confPick = await vscode.window.showOpenDialog({
    title: t('Step 3 of 4: Select the odoo.conf file'),
    canSelectFiles: true,
    defaultUri: vscode.Uri.file(current.configFile || odooPath),
    filters: { [t('Configuration files')]: ['conf', 'cfg', 'ini'], [t('All files')]: ['*'] },
    openLabel: t('Select file'),
  });
  if (!confPick?.length) {
    return false;
  }
  const configFile = confPick[0].fsPath;

  // 4. Database
  const database = await vscode.window.showInputBox({
    title: t('Step 4 of 4: Database name'),
    prompt: t('Leave empty to use db_name from odoo.conf. Use a test database, not production.'),
    value: current.database,
    ignoreFocusOut: true,
  });
  if (database === undefined) {
    return false;
  }

  await cfg.update('odooPath', odooPath, target);
  await cfg.update('pythonPath', pythonPath, target);
  await cfg.update('configFile', configFile, target);
  await cfg.update('database', database.trim(), target);
  return true;
}
