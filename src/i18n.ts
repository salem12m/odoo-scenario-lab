import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';

/**
 * Lightweight runtime i18n.
 * - Source strings are written in English and used as keys.
 * - Translations live in l10n/strings.<lang>.json (key = English text).
 * - Placeholders use {0}, {1}, ...
 * The language follows the `odooLab.language` setting, or VS Code's display language when set to "auto".
 * (Command titles and setting descriptions in package.json are translated via package.nls.<lang>.json.)
 */
export type Lang = 'en' | 'ar';
export const RTL_LANGS: Lang[] = ['ar'];

let extensionPath = '';
const cache = new Map<string, Record<string, string>>();

export function initI18n(context: vscode.ExtensionContext) {
  extensionPath = context.extensionPath;
}

export function getLang(): Lang {
  const setting = vscode.workspace.getConfiguration('odooLab').get<string>('language', 'auto');
  if (setting === 'en' || setting === 'ar') {
    return setting;
  }
  return vscode.env.language.toLowerCase().startsWith('ar') ? 'ar' : 'en';
}

export function isRtl(): boolean {
  return RTL_LANGS.includes(getLang());
}

function bundle(lang: Lang): Record<string, string> {
  if (lang === 'en') {
    return {};
  }
  if (!cache.has(lang)) {
    try {
      const file = path.join(extensionPath, 'l10n', `strings.${lang}.json`);
      cache.set(lang, JSON.parse(fs.readFileSync(file, 'utf-8')));
    } catch {
      cache.set(lang, {});
    }
  }
  return cache.get(lang)!;
}

export function t(message: string, ...args: (string | number)[]): string {
  const template = bundle(getLang())[message] ?? message;
  return template.replace(/\{(\d+)\}/g, (match, index) => (args[Number(index)] !== undefined ? String(args[Number(index)]) : match));
}

export function clearI18nCache() {
  cache.clear();
}
