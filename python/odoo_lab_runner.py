#!/usr/bin/env python3
"""
Odoo Scenario Lab - runner

Runs inside the user's Odoo Python environment. It:
  1. analyzes the selected method with `ast`
  2. loads the Odoo registry for the configured database
  3. builds scenarios (single record, multi records, empty fields, normal user ...)
  4. executes every scenario inside a SAVEPOINT that is always rolled back
  5. writes a JSON report for the VS Code extension

Nothing is ever committed to the database.
"""
import argparse
import ast
import json
import os
import signal
import sys
import time
import traceback

MAX_REPR = 240
MAX_LOCALS = 25


# --------------------------------------------------------------------------- #
# i18n: messages are written in English; translations live in python/i18n/<lang>.json
# --------------------------------------------------------------------------- #
LANG = "en"
_TRANSLATIONS = {}


def set_language(lang):
    global LANG, _TRANSLATIONS
    LANG = (lang or "en").split("-")[0].lower()
    _TRANSLATIONS = {}
    if LANG == "en":
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n", "%s.json" % LANG)
    try:
        with open(path, encoding="utf-8") as fh:
            _TRANSLATIONS = json.load(fh)
    except (OSError, ValueError):
        _TRANSLATIONS = {}


def _(message):
    return _TRANSLATIONS.get(message, message)


def norm(path):
    return os.path.normcase(os.path.realpath(path))


class LabError(Exception):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class LabTimeout(Exception):
    pass


class BlockedAction(RuntimeError):
    pass


def srepr(value):
    try:
        text = repr(value)
    except Exception as exc:  # pragma: no cover
        text = _("<cannot display value: %s>") % type(exc).__name__
    if len(text) > MAX_REPR:
        text = text[:MAX_REPR] + "…"
    return text


# --------------------------------------------------------------------------- #
# 1. Static analysis
# --------------------------------------------------------------------------- #
def _literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _decorator_name(dec):
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def analyze_source(file_path, line):
    with open(file_path, encoding="utf-8") as fh:
        source = fh.read()
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as exc:
        raise LabError("syntax", _("The file has a syntax error (SyntaxError) on line %s.") % exc.lineno, str(exc))

    target_cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.lineno <= line <= node.end_lineno:
            if target_cls is None or node.lineno > target_cls.lineno:
                target_cls = node
    if target_cls is None:
        raise LabError("no_class", _("Place the cursor inside a method of an Odoo model class."))

    target_fn = None
    for node in target_cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            if start <= line <= node.end_lineno:
                target_fn = node
    if target_fn is None:
        raise LabError("no_function", _("No method found at the cursor. Place the cursor inside the method you want to test."))

    model_name, inherits = None, []
    for stmt in target_cls.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id in ("_name", "_inherit"):
                    value = _literal(stmt.value)
                    if tgt.id == "_name" and isinstance(value, str):
                        model_name = value
                    elif tgt.id == "_inherit":
                        inherits = [value] if isinstance(value, str) else list(value or [])
    if not model_name and inherits:
        model_name = inherits[0]
    if not model_name:
        raise LabError("no_model", _("Class %s has no _name or _inherit, so it does not look like an Odoo model.") % target_cls.name)

    decorators = [_decorator_name(d) for d in target_fn.decorator_list]
    name = target_fn.name
    if name == "create" or "model_create_multi" in decorators:
        kind = "create"
    elif name == "write":
        kind = "write"
    elif "onchange" in decorators:
        kind = "onchange"
    elif "constrains" in decorators:
        kind = "constrains"
    elif "depends" in decorators or name.startswith("_compute_"):
        kind = "compute"
    elif "model" in decorators:
        kind = "model"
    else:
        kind = "records"

    fn_args = target_fn.args
    positional = [a.arg for a in fn_args.args[1:]]
    n_defaults = len(fn_args.defaults)
    required_args = positional[: len(positional) - n_defaults] if n_defaults else positional

    # names that refer to records of `self`
    aliases = {"self"}
    vals_names = {a for a in positional if a in ("vals", "values", "vals_list")}
    for node in ast.walk(target_fn):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            iter_names = {n.id for n in ast.walk(node.iter) if isinstance(n, ast.Name)}
            if "self" in iter_names:
                aliases.add(node.target.id)
            if iter_names & vals_names:
                vals_names.add(node.target.id)

    field_candidates, vals_keys, chains = [], [], []
    for node in ast.walk(target_fn):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in aliases
        ):
            pair = (node.value.attr, node.attr)
            if pair not in chains:
                chains.append(pair)
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            right = node.comparators[0]
            key = _literal(node.left)
            if isinstance(right, ast.Name) and right.id in vals_names and isinstance(key, str) and key not in vals_keys:
                vals_keys.append(key)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            if node.attr not in field_candidates:
                field_candidates.append(node.attr)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in vals_names:
            key = _literal(node.slice)
            if isinstance(key, str) and key not in vals_keys:
                vals_keys.append(key)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in vals_names
            and node.args
        ):
            key = _literal(node.args[0])
            if isinstance(key, str) and key not in vals_keys:
                vals_keys.append(key)

    exec_lines = sorted(
        {n.lineno for n in ast.walk(target_fn) if isinstance(n, ast.stmt) and n is not target_fn}
    )
    return {
        "source_lines": source.splitlines(),
        "class_name": target_cls.name,
        "model": model_name,
        "method": name,
        "kind": kind,
        "decorators": decorators,
        "positional": positional,
        "required_args": required_args,
        "field_candidates": field_candidates,
        "vals_keys": vals_keys,
        "chains": chains,
        "start": min([d.lineno for d in target_fn.decorator_list] + [target_fn.lineno]),
        "def_line": target_fn.lineno,
        "end": target_fn.end_lineno,
        "exec_lines": exec_lines,
    }


# --------------------------------------------------------------------------- #
# 2. Odoo bootstrap and compatibility helpers
# --------------------------------------------------------------------------- #
def boot_odoo(odoo_path, config_file, db_name):
    if odoo_path:
        sys.path.insert(0, odoo_path)
    try:
        try:
            import odoo.init  # noqa: F401  (Odoo 19+: core setup must be imported first)
        except ImportError:
            pass
        import odoo
        import odoo.release  # noqa: F401
        from odoo.tools import config as odoo_config
    except Exception as exc:
        raise LabError(
            "odoo_import",
            _("Could not import Odoo. Check the Odoo folder path and make sure you selected the Python where Odoo requirements are installed."),
            "%s: %s" % (type(exc).__name__, exc),
        )
    args = []
    if config_file:
        if not os.path.isfile(config_file):
            raise LabError("config_missing", _("Configuration file not found: %s") % config_file)
        args += ["-c", config_file]
    if db_name:
        args += ["-d", db_name]
    try:
        odoo_config.parse_config(args)
    except SystemExit:
        raise LabError("config_invalid", _("Could not read the Odoo configuration file."))
    for mod_name in ("odoo.modules", "odoo.modules.module"):
        try:
            module = __import__(mod_name, fromlist=["initialize_sys_path"])
            if hasattr(module, "initialize_sys_path"):
                module.initialize_sys_path()
                break
        except Exception:
            continue

    db = db_name or odoo_config.get("db_name")
    if isinstance(db, (list, tuple)):
        db = db[0] if db else None
    if isinstance(db, str) and "," in db:
        db = db.split(",")[0].strip()
    if not db:
        raise LabError("no_db", _("No database specified. Set it in the extension settings or as db_name in odoo.conf."))

    try:
        from odoo.modules.registry import Registry
    except ImportError:  # future versions
        from odoo.orm.registry import Registry  # type: ignore
    try:
        registry = Registry(db)
    except Exception as exc:
        raise LabError(
            "db_connect",
            _("Could not open database “%s”. Make sure PostgreSQL is running, the database exists, and the connection settings in odoo.conf are correct.") % db,
            "%s: %s" % (type(exc).__name__, exc),
        )
    return odoo, registry, db


def odoo_major(odoo):
    raw = str(odoo.release.version_info[0])
    try:
        return int(raw.split("~")[-1].split(".")[0])
    except ValueError:
        return 0


def env_flush(env):
    if hasattr(env, "flush_all"):
        env.flush_all()
    else:
        env["base"].flush()


def invalidate_field(records, fnames):
    if hasattr(records, "invalidate_recordset"):
        records.invalidate_recordset(fnames)
    else:
        records.invalidate_cache(fnames, records.ids)


# --------------------------------------------------------------------------- #
# 3. Tracing
# --------------------------------------------------------------------------- #
class Tracer:
    def __init__(self, target):
        self.target = target
        self.lines = set()
        self._cache = {}

    def global_trace(self, frame, event, arg):
        code = frame.f_code
        hit = self._cache.get(code)
        if hit is None:
            hit = norm(code.co_filename) == self.target
            self._cache[code] = hit
        return self.local_trace if hit else None

    def local_trace(self, frame, event, arg):
        if event == "line":
            self.lines.add(frame.f_lineno)
        return self.local_trace


def safe_locals(frame):
    out = {}
    for key, value in frame.f_locals.items():
        if key.startswith("__") or len(out) >= MAX_LOCALS:
            continue
        if type(value).__name__ in ("module", "function", "builtin_function_or_method", "type", "method"):
            continue
        out[key] = srepr(value)
    return out


# --------------------------------------------------------------------------- #
# 4. Error explanations (Arabic)
# --------------------------------------------------------------------------- #
def explain(exc, scenario, odoo_exc):
    et = type(exc).__name__
    msg = str(exc)
    ctx = scenario.get("context_hint")
    pgcode = getattr(exc, "pgcode", None)

    def res(status, category, explanation, fix):
        if ctx:
            explanation = explanation + " " + ctx
        return {"status": status, "category": category, "explanation": explanation, "fix": fix}

    if isinstance(exc, LabTimeout):
        return res("fail", _("Timeout"), _("Execution took longer than the allowed timeout, most likely an infinite loop or too many queries."),
                   _("Check your loop conditions, and call search once instead of inside a loop."))
    if isinstance(exc, BlockedAction):
        return res("warn", _("External action"), _("The code attempted an action that was blocked during testing: %s") % msg,
                   _("This is a safety block by the extension, not necessarily a bug in your code. Make sure the action is intended."))
    if odoo_exc["AccessError"] and isinstance(exc, odoo_exc["AccessError"]):
        return res("warn", _("Access rights"), _("The user in this scenario is not allowed to perform the operation: %s") % msg,
                   _("If regular users should be able to do this, add ir.model.access or record rules, or use sudo() carefully on the specific part only."))
    if odoo_exc["UserError"] and isinstance(exc, odoo_exc["UserError"]):
        return res("expected", _("Intended rejection"), _("The code rejected this case with a user message: “%s”.") % msg,
                   _("This is usually correct behavior. Just make sure the message is clear and the rejection is intended in this case."))
    if odoo_exc["MissingError"] and isinstance(exc, odoo_exc["MissingError"]):
        return res("fail", _("Missing record"), _("The code tried to read a deleted or non-existent record."),
                   _("Use records.exists() before reading when the record may have been deleted."))

    if pgcode == "23502":
        return res("fail", _("Required field"), _("When saving to the database, a required (NOT NULL) field was left empty: %s") % msg.splitlines()[0],
                   _("Make sure the field is filled before saving, or give it a default value in the field definition."))
    if pgcode == "23503":
        return res("fail", _("Broken relation"), _("A non-existent record was linked, or a record referenced by others was deleted (foreign key)."),
                   _("Check related records before deleting or linking, or review ondelete in the field definition."))
    if pgcode == "23505":
        return res("fail", _("Duplicate value"), _("A value that must be unique was duplicated."),
                   _("Check that the value does not exist before creating, or raise a clear ValidationError."))
    if pgcode == "42703":
        return res("fail", _("Database not up to date"), _("The code uses a field that does not exist yet in the database tables."),
                   _("Update the module by running Odoo with -u your_module, then test again."))

    if isinstance(exc, ValueError) and "Expected singleton" in msg:
        return res("fail", _("Multiple records"), _("The method treated several records as a single record (%s). In Odoo, self may contain multiple records, e.g. when the action is run from a list view.") % msg.replace("Expected singleton: ", ""),
                   _("Loop over the records with for rec in self: and use rec instead of self, or add self.ensure_one() if the method is meant for a single record."))
    if isinstance(exc, ValueError) and ("failed to assign" in msg or "Compute method" in msg):
        return res("fail", _("Computed field"), _("The compute method did not assign a value to the field in some cases (usually an if branch that assigns nothing)."),
                   _("Assign a default value at the start of the loop, e.g. rec.field_name = False, then apply your conditions."))
    if isinstance(exc, AttributeError) and "'bool' object has no attribute" in msg:
        return res("fail", _("Empty value"), _("False was used as if it were an object (%s). Empty fields in Odoo return False, not a record or a string.") % msg,
                   _("Check the value before using it, e.g. if rec.date_field: or (rec.name or '')."))
    if isinstance(exc, AttributeError) and "'NoneType' object has no attribute" in msg:
        return res("fail", _("Empty value"), _("None was used as if it were an object (%s).") % msg,
                   _("Check the value before using it, especially values from vals.get() or functions that may return nothing."))
    if isinstance(exc, AttributeError):
        return res("fail", _("Unknown name"), _("A field or method that does not exist was used: %s") % msg,
                   _("Check the spelling, and make sure the module defining the field is listed in depends in __manifest__.py and installed."))
    if isinstance(exc, TypeError) and ("bool" in msg or "NoneType" in msg):
        return res("fail", _("Arithmetic on empty value"), _("An arithmetic operation or comparison was done on an empty value (%s).") % msg,
                   _("Use a fallback value such as (rec.amount or 0.0), or check the date before comparing it."))
    if isinstance(exc, TypeError):
        return res("fail", _("Unexpected type"), _("A value of an unexpected type was passed: %s") % msg,
                   _("Check the variable types on the highlighted line and the number of arguments passed to the function."))
    if isinstance(exc, ZeroDivisionError):
        return res("fail", _("Division by zero"), _("A division by zero occurred."),
                   _("Check before dividing: value / total if total else 0.0 (for floats use float_is_zero)."))
    if isinstance(exc, KeyError):
        return res("fail", _("Missing key"), _("Key %s is not in the dictionary. In write and create, vals only contains the fields that changed.") % msg,
                   _("Use vals.get('field_name') and handle the case where the key is missing."))
    if isinstance(exc, IndexError):
        return res("fail", _("Empty recordset"), _("The code accessed an element that does not exist, such as records[0] on an empty recordset."),
                   _("Check first: if records: before accessing the element."))
    return res("fail", et, _("A %s error occurred: %s") % (et, msg), _("Review the highlighted line and the variable values at the moment of the error."))


# --------------------------------------------------------------------------- #
# 5. Scenario generation
# --------------------------------------------------------------------------- #
FIELD_TYPE_LABEL = {
    "many2one": "Many2one", "char": "Char", "text": "Text", "html": "Html", "integer": "Integer",
    "float": "Float", "monetary": "Monetary", "date": "Date", "datetime": "Datetime",
    "selection": "Selection", "boolean": "Boolean", "one2many": "One2many", "many2many": "Many2many",
}


def field_variations(field, current):
    t = field.type
    if t == "many2one":
        return [(False, _("empty (False)"))]
    if t in ("char", "text", "html"):
        return [(False, _("empty (False)")), ("", _("empty string ''"))]
    if t in ("integer", "float", "monetary"):
        return [(0, _("zero")), (-1, _("negative value -1"))]
    if t in ("date", "datetime"):
        return [(False, _("empty (False)"))]
    if t == "boolean":
        return [(not current, str(not current))]
    if t in ("one2many", "many2many"):
        return [("__clear__", _("No records"))]
    return []


def build_scenarios(env, info, max_scenarios, login):
    kind = info["kind"]
    Model = env[info["model"]]
    fields_desc = Model.fields_get()
    used_fields = [f for f in info["field_candidates"] if f in Model._fields][:8]
    vals_keys = [k for k in info["vals_keys"] if k in Model._fields][:8]
    warnings = []

    if getattr(Model, "_abstract", False) and kind not in ("model",):
        warnings.append(_("This is an abstract model (AbstractModel) with no records, so testing is limited."))

    stored_filters = [
        (f, "!=", False) for f in used_fields
        if Model._fields[f].store and Model._fields[f].type not in ("one2many", "many2many", "boolean", "binary")
    ][:3]
    base = Model.browse()
    multi = Model.browse()
    if not getattr(Model, "_abstract", False):
        try:
            base = Model.search(stored_filters, limit=1, order="id desc") or Model.search([], limit=1, order="id desc")
            multi = Model.search([], limit=3, order="id desc")
        except Exception as exc:
            warnings.append(_("Could not fetch records for testing: %s") % exc)
    if not base and kind not in ("model", "create"):
        warnings.append(_("There are no %s records in the database. Create at least one record for more thorough testing.") % info["model"])

    normal_user = env["res.users"]
    try:
        admin = env.ref("base.user_admin", raise_if_not_found=False)
        domain = [("share", "=", False), ("id", "not in", [1, admin.id if admin else 0, env.uid])]
        normal_user = env["res.users"].search(domain, limit=1)
        if normal_user and normal_user.has_group("base.group_system"):
            normal_user = env["res.users"].browse()
    except Exception:
        pass

    method = info["method"]
    compute_fields = [name for name, f in Model._fields.items() if f.compute == method]
    scenarios = []

    def extra_args():
        args, shown = [], {}
        for name in info["required_args"]:
            if name in ("vals", "values"):
                value = {}
            elif name == "vals_list":
                value = [{}]
            elif name in ("domain",):
                value = []
            else:
                value = None
            args.append(value)
            shown[name] = srepr(value)
        return args, shown

    def add(title, description, inputs, prepare, context_hint=None):
        if len(scenarios) < max_scenarios:
            scenarios.append({
                "title": title, "description": description, "inputs": inputs,
                "prepare": prepare, "context_hint": context_hint,
            })

    def call_method(records, args):
        if kind == "compute" and compute_fields:
            def run():
                field = records._fields[compute_fields[0]]
                field.compute_value(records)
                return {fname: [srepr(r[fname]) for r in records[:5]] for fname in compute_fields}
            return run
        return lambda: getattr(records, method)(*args)

    def selection_values(fname, current):
        sel = fields_desc.get(fname, {}).get("selection") or []
        return [(key, "'%s'" % key) for key, _label in sel if key != current][:5]

    def variations(fname, record):
        field = Model._fields[fname]
        current = record[fname] if record else False
        if field.type == "selection":
            return selection_values(fname, current)
        return field_variations(field, current)

    # ----- records-based kinds ------------------------------------------------
    if kind in ("records", "compute", "constrains", "onchange", "write"):
        args, shown_args = extra_args()

        def on(records, label, **extra):
            data = {_("Records"): srepr(records)}
            data.update(shown_args)
            data.update(extra)
            return data

        if kind == "onchange":
            if base:
                add(_("Record in edit mode"), _("Simulates opening record %s in the form view and running onchange.") % base.display_name,
                    on(base, ""), lambda: call_method(Model.new(origin=base), args))
            add(_("New empty record"), _("Simulates clicking “New” and running onchange before any field is filled."),
                {_("Records"): "%s.new({})" % info["model"]}, lambda: call_method(Model.new({}), args),
                _("In this scenario the record is new and all its fields are empty."))
        elif kind == "write":
            if base:
                add(_("Write without values"), _("Calls write({}) on a single record."), on(base, "", vals="{}"),
                    lambda: (lambda: base.write({})))
            if len(multi) > 1:
                for key in vals_keys[:3]:
                    add(_("Write %s on multiple records") % key, _("Writes %s on %d records at the same time.") % (key, len(multi)),
                        on(multi, "", vals="{'%s': ...}" % key),
                        lambda key=key: (lambda: multi.write(Model._convert_to_write({key: multi[0][key]}))),
                        _("In this scenario self contains %d records.") % len(multi))
        else:
            if base:
                add(_("Single record"), _("Runs the method on record %s as it is in the database.") % base.display_name,
                    on(base, ""), lambda: call_method(base, args))
            if len(multi) > 1:
                add(_("Multiple records together"), _("Runs the method on %d records at once, as happens when running it from a list view.") % len(multi),
                    on(multi, ""), lambda: call_method(multi, args),
                    _("In this scenario self contains %d records.") % len(multi))
            add(_("No records"), _("Runs the method on an empty recordset."), on(Model.browse(), ""),
                lambda: call_method(Model.browse(), args), _("In this scenario self is empty."))

        # field variations
        keys = vals_keys if kind == "write" else [f for f in used_fields if f not in compute_fields]
        for fname in keys:
            field = Model._fields[fname]
            for value, label in variations(fname, base):
                title = _("Field %s: %s") % (fname, label)
                hint = _("In this scenario field %s is %s.") % (fname, label)
                inputs = {_("Records"): srepr(base) if base else _("new record"), fname: label}
                write_value = [(5, 0, 0)] if value == "__clear__" else value

                if kind in ("onchange", "compute"):
                    def prep(fname=fname, write_value=write_value):
                        rec = Model.new(origin=base) if base else Model.new({})
                        rec[fname] = write_value
                        return call_method(rec, args)
                    add(title, _("Temporary copy of the record with %s changed (%s), then runs the method.") % (fname, FIELD_TYPE_LABEL.get(field.type, field.type)), inputs, prep, hint)
                elif kind == "write":
                    if base:
                        add(title, _("Calls write with %s = %s.") % (fname, label), dict(inputs, vals="{'%s': %s}" % (fname, srepr(write_value))),
                            lambda fname=fname, write_value=write_value: (lambda: base.write({fname: write_value})), hint)
                elif base and (field.store or field.inverse):
                    def prep(fname=fname, write_value=write_value):
                        base.write({fname: write_value})
                        return call_method(base, args)
                    add(title, _("Changes %s on the record (temporarily), then runs the method.") % fname, inputs, prep, hint)

        if base and kind != "write":
            for rel, sub in info["chains"][:6]:
                rel_field = Model._fields.get(rel)
                if not rel_field or rel_field.type != "many2one" or rel in compute_fields:
                    continue
                Comodel = env[rel_field.comodel_name]
                sub_field = Comodel._fields.get(sub)
                if not sub_field or not sub_field.store or sub_field.required or sub_field.type in ("one2many", "many2many", "boolean"):
                    continue
                if not base[rel]:
                    continue
                label = _("%s.%s empty") % (rel, sub)

                def prep(rel=rel, sub=sub):
                    base[rel].write({sub: False})
                    target_rec = Model.new(origin=base) if kind == "onchange" else base
                    return call_method(target_rec, args)
                add(_("Field %s") % label, _("Clears %s on the related record %s (temporarily), then runs the method.") % (sub, base[rel].display_name),
                    {_("Records"): srepr(base), "%s.%s" % (rel, sub): "False"}, prep,
                    _("In this scenario the related record %s has no value in %s.") % (rel, sub))

        if normal_user and base:
            add(_("Regular user"), _("Runs the method with the permissions of user %s instead of the administrator.") % normal_user.login,
                {_("Records"): srepr(base), _("User"): normal_user.login},
                lambda: call_method((Model.new(origin=base) if kind == "onchange" else base).with_user(normal_user), args),
                _("In this scenario the method is run by a regular user (%s).") % normal_user.login)

    # ----- create ---------------------------------------------------------------
    elif kind == "create":
        template = {}
        if base:
            try:
                template = base.copy_data()[0]
            except Exception:
                template = {}
        if template:
            add(_("Complete data"), _("Creates a record with data copied from record %s.") % base.display_name,
                {"vals": _("copy of %s") % srepr(base)}, lambda: (lambda: Model.create(dict(template))))
        add(_("No data"), _("Calls create({}) without any values."), {"vals": "{}"},
            lambda: (lambda: Model.create({})), _("In this scenario vals is completely empty."))
        if template:
            add(_("Create multiple records"), _("Calls create with a list of two records."), {"vals_list": "[vals, vals]"},
                lambda: (lambda: Model.create([dict(template), dict(template)])), _("In this scenario two records are created at once."))
        for key in vals_keys:
            vals = dict(template)
            vals.pop(key, None)
            add(_("Without field %s") % key, _("Creates a record without the key %s in vals.") % key, {"vals": _("without '%s'") % key},
                lambda vals=vals: (lambda: Model.create(dict(vals))), _("In this scenario vals does not contain '%s'.") % key)
            vals2 = dict(template)
            vals2[key] = False
            add(_("Field %s = False") % key, _("Creates a record with field %s set to False.") % key, {"vals": "'%s': False" % key},
                lambda vals2=vals2: (lambda: Model.create(dict(vals2))), _("In this scenario '%s' is False.") % key)
        if normal_user:
            add(_("Regular user"), _("Creates a record with the permissions of %s.") % normal_user.login, {_("User"): normal_user.login},
                lambda: (lambda: Model.with_user(normal_user).create(dict(template))),
                _("In this scenario the method is run by a regular user (%s).") % normal_user.login)

    # ----- @api.model -----------------------------------------------------------
    else:
        args, shown = extra_args()
        add(_("Default call"), _("Calls the method directly on the model."), shown or {_("Arguments"): _("none")},
            lambda: (lambda: getattr(Model, method)(*args)))
        for i, name in enumerate(info["required_args"]):
            for value, label in ((None, "None"), (False, "False"), ("", _("empty string"))):
                new_args = list(args)
                new_args[i] = value
                add(_("Argument %s = %s") % (name, label), _("Calls the method with %s = %s.") % (name, label), {name: label},
                    lambda new_args=new_args: (lambda: getattr(Model, method)(*new_args)),
                    _("In this scenario argument %s is %s.") % (name, label))
        if normal_user:
            add(_("Regular user"), _("Calls the method with the permissions of %s.") % normal_user.login, {_("User"): normal_user.login},
                lambda: (lambda: getattr(Model.with_user(normal_user), method)(*args)),
                _("In this scenario the method is run by a regular user (%s).") % normal_user.login)

    return scenarios, warnings, used_fields, vals_keys


# --------------------------------------------------------------------------- #
# 6. Execution
# --------------------------------------------------------------------------- #
def install_guards(odoo, env, cr, notes):
    def blocked_commit(*_a, **_k):
        notes.add(_("The code calls cr.commit(); it was ignored during testing. Avoid commit inside model methods."))

    def blocked_rollback(*_a, **_k):
        raise BlockedAction(_("cr.rollback() inside the code"))

    cr.commit = blocked_commit
    cr.rollback = blocked_rollback

    try:
        MailServer = type(env["ir.mail_server"])

        def fake_send(self, message, *a, **k):
            notes.add(_("The code tried to send an email; it was blocked during testing."))
            return message.get("Message-Id") or "<odoo-lab>"

        MailServer.send_email = fake_send
    except Exception:
        pass

    try:
        import requests

        def blocked_request(self, method, url, *a, **k):
            raise BlockedAction(_("external request to %s") % url)

        requests.sessions.Session.request = blocked_request
    except Exception:
        pass


def execute_scenario(odoo, env, cr, info, scenario, target, timeout, odoo_exc):
    tracer = Tracer(target)
    phase = "setup"
    result = {
        "title": scenario["title"], "description": scenario["description"],
        "inputs": scenario["inputs"], "status": "pass", "durationMs": 0,
    }
    use_alarm = hasattr(signal, "SIGALRM") and timeout > 0

    def on_alarm(_sig, _frm):
        raise LabTimeout()

    started = time.time()
    cr.execute("SAVEPOINT odoo_lab")
    try:
        if use_alarm:
            signal.signal(signal.SIGALRM, on_alarm)
            signal.setitimer(signal.ITIMER_REAL, timeout)
        sys.settrace(tracer.global_trace)
        try:
            runner = scenario["prepare"]()
            phase = "call"
            returned = runner()
            phase = "flush"
            env_flush(env)
        finally:
            sys.settrace(None)
            if use_alarm:
                signal.setitimer(signal.ITIMER_REAL, 0)
        result["returned"] = srepr(returned)
    except Exception as exc:
        tb = exc.__traceback__
        user_tb, last_tb, passed = None, None, False
        walker = tb
        while walker is not None:
            if norm(walker.tb_frame.f_code.co_filename) == target:
                user_tb = walker
                if info["start"] <= walker.tb_lineno <= info["end"]:
                    passed = True
            last_tb = walker
            walker = walker.tb_next
        touched = bool(tracer.lines & set(range(info["def_line"] + 1, info["end"] + 1)))

        if phase == "setup" and not passed and not touched:
            result["status"] = "skipped"
            result["skipReason"] = _("Could not prepare the scenario before reaching your method: %s: %s") % (type(exc).__name__, str(exc).splitlines()[0] if str(exc) else "")
        else:
            verdict = explain(exc, scenario, odoo_exc)
            result["status"] = verdict["status"]
            error = {
                "type": type(exc).__name__,
                "message": str(exc)[:1500],
                "category": verdict["category"],
                "explanation": verdict["explanation"],
                "fix": verdict["fix"],
                "phase": phase,
                "traceback": "".join(traceback.format_exception(type(exc), exc, tb))[-5000:],
            }
            if user_tb is not None:
                lineno = user_tb.tb_lineno
                error["userLine"] = lineno
                error["userCode"] = info["source_lines"][lineno - 1].strip() if lineno <= len(info["source_lines"]) else ""
                error["inUserMethod"] = info["start"] <= lineno <= info["end"]
                error["locals"] = safe_locals(user_tb.tb_frame)
            elif phase == "flush":
                error["note"] = _("The error appeared after your method finished, while saving changes to the database.")
            if last_tb is not None:
                error["raisedAt"] = {
                    "file": last_tb.tb_frame.f_code.co_filename,
                    "line": last_tb.tb_lineno,
                    "function": last_tb.tb_frame.f_code.co_name,
                }
            result["error"] = error
    finally:
        try:
            cr.execute("ROLLBACK TO SAVEPOINT odoo_lab")
        except Exception:
            cr._cnx.rollback()
        env.clear()

    result["durationMs"] = int((time.time() - started) * 1000)
    covered = sorted(l for l in tracer.lines if info["def_line"] < l <= info["end"])
    result["coveredLines"] = covered
    if result["status"] == "pass" and not covered and info["kind"] != "compute":
        result["status"] = "notreached"
        result["skipReason"] = _("The scenario finished without errors, but no line of your method was executed (another override may bypass it, or a condition prevented it).")
    return result


def check(args):
    odoo, registry, db = boot_odoo(args.odoo_path, args.config, args.db)
    with registry.cursor() as cr:
        cr.execute("SELECT count(*) FROM ir_module_module WHERE state = 'installed'")
        installed = cr.fetchone()[0]
    return {"odooVersion": odoo.release.version, "database": db, "installedModules": installed,
            "python": sys.version.split()[0]}


def find_module_root(file_path):
    folder = os.path.dirname(os.path.abspath(file_path))
    while True:
        if os.path.isfile(os.path.join(folder, "__manifest__.py")) or os.path.isfile(os.path.join(folder, "__openerp__.py")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def load_diagnostics(odoo, env, cr, info, file_path, target):
    import difflib
    Model = env[info["model"]]
    model_files = []
    for klass in type(Model).__mro__:
        module = sys.modules.get(getattr(klass, "__module__", "") or "")
        module_file = getattr(module, "__file__", None)
        if module_file and norm(module_file) not in [norm(f) for f in model_files]:
            model_files.append(module_file)
    root = find_module_root(file_path)
    module_name = os.path.basename(root) if root else None
    loaded_from = None
    state = None
    if module_name:
        loaded = sys.modules.get("odoo.addons.%s" % module_name)
        loaded_file = getattr(loaded, "__file__", None)
        if loaded_file:
            loaded_from = os.path.dirname(loaded_file)
        try:
            cr.execute("SELECT state FROM ir_module_module WHERE name = %s", (module_name,))
            row = cr.fetchone()
            state = row[0] if row else "not found"
        except Exception:
            state = None
    methods = [name for name in dir(type(Model)) if not name.startswith("__")]
    return {
        "odooVersion": odoo.release.version,
        "odooSource": os.path.dirname(os.path.dirname(os.path.abspath(odoo.release.__file__))),
        "addonsPath": odoo_config_value("addons_path"),
        "moduleName": module_name,
        "moduleState": state,
        "moduleLoadedFrom": loaded_from,
        "moduleLoadedFromTarget": bool(loaded_from and root and norm(loaded_from) == norm(root)),
        "fileLoaded": target in {norm(f) for f in model_files},
        "modelFiles": model_files[:15],
        "similarMethods": difflib.get_close_matches(info["method"], methods, n=5, cutoff=0.6),
    }


def odoo_config_value(key):
    try:
        from odoo.tools import config as odoo_config
        value = odoo_config.get(key)
        return ",".join(value) if isinstance(value, (list, tuple)) else value
    except Exception:
        return None


def format_diagnostics(diag):
    lines = [
        "Odoo %s  (%s)" % (diag["odooVersion"], diag["odooSource"]),
        "addons_path: %s" % diag["addonsPath"],
        "module: %s  state: %s" % (diag["moduleName"], diag["moduleState"]),
        "module loaded from: %s" % diag["moduleLoadedFrom"],
        "this file is part of the model: %s" % diag["fileLoaded"],
        "model defined in:",
    ]
    lines += ["  - %s" % f for f in diag["modelFiles"]]
    if diag["similarMethods"]:
        lines.append("similar methods: %s" % ", ".join(diag["similarMethods"]))
    return "\n".join(lines)


def run(args):
    target = norm(args.file)
    info = analyze_source(args.file, args.line)
    odoo, registry, db = boot_odoo(args.odoo_path, args.config, args.db)

    if not args.allow_prod and any(w in db.lower() for w in ("prod", "live")):
        raise LabError("production_db", _("Database “%s” looks like a production database. Use a test copy, or allow it in the settings if you are sure.") % db)

    from odoo import api
    import odoo.exceptions as oexc
    SUPERUSER_ID = getattr(api, "SUPERUSER_ID", 1)
    odoo_exc = {
        "AccessError": getattr(oexc, "AccessError", None),
        "UserError": getattr(oexc, "UserError", None),
        "MissingError": getattr(oexc, "MissingError", None),
    }

    report = {
        "odooVersion": odoo.release.version, "database": db, "model": info["model"],
        "method": info["method"], "kind": info["kind"], "className": info["class_name"],
        "file": args.file, "range": [info["start"], info["end"]], "execLines": info["exec_lines"],
        "warnings": [], "notes": [],
    }

    cr = registry.cursor()
    notes = set()
    try:
        env = api.Environment(cr, SUPERUSER_ID, {})
        if info["model"] not in env:
            raise LabError("model_missing", _("Model “%s” does not exist in database “%s”. Is the module installed? If it is new, install it or update it with -u.") % (info["model"], db))

        user = env["res.users"]
        if args.user:
            user = env["res.users"].search([("login", "=", args.user)], limit=1)
            if not user:
                raise LabError("user_missing", _("User “%s” does not exist.") % args.user)
        else:
            user = env.ref("base.user_admin", raise_if_not_found=False) or env["res.users"].browse(SUPERUSER_ID)
        context = user.context_get() if hasattr(user, "context_get") else {}
        env = api.Environment(cr, user.id, dict(context))
        report["runAs"] = user.login

        diag = load_diagnostics(odoo, env, cr, info, args.file, target)
        report["loadInfo"] = diag
        if diag["moduleName"] and diag["moduleLoadedFrom"] and not diag["moduleLoadedFromTarget"]:
            raise LabError(
                "module_other_path",
                _("Odoo loaded module “%s” from another folder: %s — not from the file you are testing. Fix the order of addons_path in odoo.conf, or remove the duplicate copy.")
                % (diag["moduleName"], diag["moduleLoadedFrom"]),
                format_diagnostics(diag),
            )
        if getattr(type(env[info["model"]]), info["method"], None) is None:
            if diag["moduleName"] and diag["moduleState"] not in (None, "installed"):
                message = _("Module “%s” is in state “%s” in this database, so its code is not loaded. Install or update it first.") % (diag["moduleName"], diag["moduleState"])
            elif not diag["fileLoaded"]:
                message = _("Method %s is missing because this file is not part of the loaded model %s. Check that the file is imported in models/__init__.py.") % (info["method"], info["model"])
            else:
                message = _("Method %s does not exist on the loaded model. Is the file imported in __init__.py and the module updated?") % info["method"]
            raise LabError("method_missing", message, format_diagnostics(diag))
        if not diag["fileLoaded"]:
            report["warnings"].append(_("This file is not loaded as part of model %s in the database. Make sure the module is installed and the file is imported in __init__.py, otherwise the test will not run your code.") % info["model"])

        install_guards(odoo, env, cr, notes)
        scenarios, warnings, used, keys = build_scenarios(env, info, args.max_scenarios, report["runAs"])
        report["warnings"].extend(warnings)
        report["fieldsDetected"] = used
        report["valsKeysDetected"] = keys

        results = []
        for scenario in scenarios:
            results.append(execute_scenario(odoo, env, cr, info, scenario, target, args.timeout, odoo_exc))
        report["scenarios"] = results
        covered = set()
        for r in results:
            covered.update(r.get("coveredLines", []))
        report["coveredLines"] = sorted(covered)
        report["notes"] = sorted(notes)
    finally:
        for attr in ("commit", "rollback"):
            cr.__dict__.pop(attr, None)
        try:
            cr.rollback()
        finally:
            cr.close()
    return report


def main():
    parser = argparse.ArgumentParser(description="Odoo Scenario Lab runner")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--file", default="")
    parser.add_argument("--line", type=int, default=0)
    parser.add_argument("--out", required=True)
    parser.add_argument("--odoo-path", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--db", default="")
    parser.add_argument("--user", default="")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--max-scenarios", type=int, default=30)
    parser.add_argument("--allow-prod", action="store_true")
    parser.add_argument("--lang", default="en")
    args = parser.parse_args()
    set_language(args.lang)

    started = time.time()
    try:
        data = check(args) if args.check else run(args)
        data["ok"] = True
    except LabError as exc:
        data = {"ok": False, "error": {"code": exc.code, "message": exc.message, "detail": exc.detail}}
    except Exception as exc:
        data = {"ok": False, "error": {"code": "internal", "message": _("Internal extension error: %s") % exc,
                                        "detail": traceback.format_exc()}}
    data["totalMs"] = int((time.time() - started) * 1000)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, default=str)


if __name__ == "__main__":
    main()
