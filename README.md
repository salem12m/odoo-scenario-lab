# Odoo Scenario Lab

> [العربية](README.ar.md)

Test any Odoo model method with one click. The extension generates realistic scenarios from your own database, runs them safely, and gives you a report showing **where** the error happens, **why**, and **how to fix it**.

> ⚠️ **Preview:** this extension is under active testing. Feedback and bug reports are welcome.

## What it catches
- `Expected singleton` when a method runs on several records.
- Using an empty field (`False`) as if it were a record, string or date.
- Division by zero and arithmetic on empty values.
- Missing keys in `vals` inside `create` and `write`.
- Compute methods that do not assign a value in every branch.
- Access errors when a regular user runs the method.
- Fields that exist in code but not in the database (module needs an update).
- Module loaded from a different folder than the file you are editing.

## Usage
1. Run **Odoo Lab: Configure Odoo connection** and pick the Odoo folder, Python, `odoo.conf` and database.
2. Click **▶ Test scenarios** above any method, or right-click → *Test method scenarios*, or press `Ctrl+Alt+T`.
3. The report opens beside your code, and failing lines are highlighted in red.

## Scenarios
| Method type | What is tried |
|---|---|
| Button / action | single record, multiple records, empty recordset, empty value for each used field, Selection values, empty related fields, regular user |
| `@api.depends` | the same on a temporary copy of the record |
| `@api.onchange` | existing record, new empty record, emptied fields |
| `@api.constrains` | zero, negative and empty values |
| `create` | complete data, `{}`, multi-create, each key removed or set to `False` |
| `write` | writing on several records at once and different values per key |

## Safety
- Every scenario runs inside a `SAVEPOINT` and is fully rolled back; nothing is saved to the database.
- `cr.commit()` is ignored, and outgoing emails and external HTTP requests are blocked during tests.
- Databases whose name contains `prod` or `live` are refused by default.
- Still: **always use a test copy of your database.**

## Requirements
- Odoo 14 or later installed locally (no Docker support yet). Tested on Odoo 17 and 19.
- PostgreSQL running, and a database where the module is installed.
- The module updated after adding new fields (`-u your_module`).
- For automatic interpreter detection: the Microsoft Python extension.

## Settings
| Setting | Description |
|---|---|
| `odooLab.pythonPath` | Python with Odoo requirements (empty = interpreter selected in VS Code) |
| `odooLab.odooPath` | Odoo folder containing `odoo-bin` |
| `odooLab.configFile` | `odoo.conf` file (supports `${workspaceFolder}`) |
| `odooLab.database` | Database (empty = `db_name` from the config file) |
| `odooLab.runAsUser` | User login that runs the scenarios (default admin) |
| `odooLab.timeoutSeconds` | Timeout per scenario |
| `odooLab.maxScenarios` | Maximum scenarios per test |
| `odooLab.allowProductionDatabase` | Allow production-like database names |
| `odooLab.enableCodeLens` | Show the test button above methods |
| `odooLab.language` | Report language: `auto`, `en` or `ar` |

> Tip: keep `odooPath`, `configFile` and `database` in each project's `.vscode/settings.json` rather than in user settings, so every project uses its own database.

## Translations
- UI strings: `l10n/strings.<lang>.json`
- Command titles and settings: `package.nls.<lang>.json`
- Report messages from the Python runner: `python/i18n/<lang>.json`

All keys are the English source strings. To add a language, copy the Arabic files, translate the values, and add the language code to `odooLab.language`.
