# Changelog

## 0.2.0
- English is now the default language, with a full Arabic translation (`odooLab.language`: auto / en / ar).
- Clear diagnostics when a method is not found: shows where Odoo loaded the module from, module state, and similar method names.
- Detects when Odoo loads the module from a different folder than the edited file (duplicate module in `addons_path`).
- Syntax warnings now show the real file name.
- Removed the fallback to `python.defaultInterpreterPath` (it often pointed to a plain `python`).

## 0.1.1
- Odoo 19 support. Tested on Odoo 17 and 19.
- Uses the interpreter selected in VS Code (Python: Select Interpreter) when `pythonPath` is empty.

## 0.1.0
- First release: scenario testing for Odoo model methods, safe rollback, report with error location, variable values, explanation and fix.
