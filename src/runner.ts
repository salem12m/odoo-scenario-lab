import { spawn } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';
import { LabConfig } from './config';
import { getLang, t } from './i18n';

export interface RunnerResult {
  ok: boolean;
  error?: { code: string; message: string; detail?: string | null };
  [key: string]: any;
}

export function runRunner(
  context: vscode.ExtensionContext,
  config: LabConfig,
  extraArgs: string[],
  output: vscode.OutputChannel,
  token: vscode.CancellationToken,
): Promise<RunnerResult> {
  const script = context.asAbsolutePath(path.join('python', 'odoo_lab_runner.py'));
  const outFile = path.join(os.tmpdir(), `odoo-lab-${process.pid}-${Date.now()}.json`);
  const args = [
    script,
    '--out', outFile,
    '--lang', getLang(),
    '--odoo-path', config.odooPath,
    '--config', config.configFile,
    '--timeout', String(config.timeoutSeconds),
    '--max-scenarios', String(config.maxScenarios),
  ];
  if (config.database) {
    args.push('--db', config.database);
  }
  if (config.runAsUser) {
    args.push('--user', config.runAsUser);
  }
  if (config.allowProductionDatabase) {
    args.push('--allow-prod');
  }
  args.push(...extraArgs);

  output.appendLine(`\n▶ ${config.pythonPath} ${args.map((a) => (a.includes(' ') ? `"${a}"` : a)).join(' ')}`);

  return new Promise((resolve) => {
    let settled = false;
    const finish = (result: RunnerResult) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(killer);
      fs.rm(outFile, { force: true }, () => undefined);
      resolve(result);
    };

    const child = spawn(config.pythonPath, args, {
      cwd: config.odooPath || undefined,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
    });

    const overall = (config.timeoutSeconds * (config.maxScenarios + 3) + 120) * 1000;
    const killer = setTimeout(() => {
      child.kill();
      finish({ ok: false, error: { code: 'overall_timeout', message: t('The test took longer than allowed and was stopped.') } });
    }, overall);

    token.onCancellationRequested(() => {
      child.kill();
      finish({ ok: false, error: { code: 'cancelled', message: t('The test was cancelled.') } });
    });

    child.stdout.on('data', (d) => output.append(d.toString()));
    child.stderr.on('data', (d) => output.append(d.toString()));

    child.on('error', (err: NodeJS.ErrnoException) => {
      const message = err.code === 'ENOENT'
        ? t('Python was not found at: {0}', config.pythonPath)
        : t('Could not start Python: {0}', err.message);
      finish({ ok: false, error: { code: 'spawn', message } });
    });

    child.on('close', (code) => {
      if (settled) {
        return;
      }
      try {
        const raw = fs.readFileSync(outFile, 'utf-8');
        finish(JSON.parse(raw));
      } catch {
        finish({
          ok: false,
          error: {
            code: 'no_output',
            message: t('The runner stopped without a result (exit code {0}). See Output → Odoo Scenario Lab for details.', String(code)),
          },
        });
      }
    });
  });
}
