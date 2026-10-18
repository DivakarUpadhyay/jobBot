"""Settings, kept in the database table `settings` (one row per setting).

config.yaml is only the list of defaults: a setting that is new in the file is added to the table,
and "reset" goes back to the file's value. A value in the table is never changed by the file.
Change settings on the admin page or with `PUT /config`."""
import json
import re

import yaml

from . import APP, home

MASTER_FILE = APP / "config.yaml"                 # every setting there is, each with its explanation
NEW_ACCOUNT_FILE = APP / "defaults" / "config.yaml"   # what a new account starts with where that differs

# Top-level settings that are one table keyed by country: kept as a single value, not split up.
WHOLE = ("exclude_countries", "remote_only_countries")


# Pipeline steps that can be switched on and off (admin page, API, or `jobbot process`).
PROCESSES = {
    "bot": "Master switch: when off, an iteration does nothing at all",
    "clean": "Delete old generated PDFs before the run",
    "discover": "Look for new job sources country by country",
    "scrape": "Read new jobs from the active sources",
    "enrich": "Read full job pages (104) for description and contact",
    "contacts": "Look for HR emails and company websites",
    "match": "Pick the best jobs of the day, 50 full-time + 50 part-time",
    "tailor": "Write the resume and email for each job from its description",
    "inbox": "Read bounces and replies from the sender inboxes",
    "send": "Send approved application emails",
    "leads": "Send approved lead (tender) emails",
    "prospects": "Send approved speculative applications to Taiwan companies",
    "followup": "One follow-up, a week after an application, when nothing came back",
    "report": "Write the Excel job list, the tracker and the date-wise CSV",
}


def _default_files():
    """The files this account's defaults come from: its own config.yaml, else the master list and the new-account one."""
    own = home() / "config.yaml"
    return [own] if own.exists() else [f for f in (MASTER_FILE, NEW_ACCOUNT_FILE) if f.exists()]


def file_config():
    """The default value of every setting for the current account."""
    cfg = {}
    for path in _default_files():
        for key, value in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict) and key not in WHOLE:
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


def _stamp():
    return "|".join(str(f.stat().st_mtime_ns) for f in _default_files())


def _leaves(cfg):
    """(key, value) for every setting: `section.name`, or just `name` for one at the top level."""
    for key, value in cfg.items():
        if isinstance(value, dict) and value and key not in WHOLE:
            for sub, v in value.items():
                yield f"{key}.{sub}", v
        else:
            yield key, value


def _comments(text):
    """{key: explanation} from the comments in config.yaml: the lines right above a setting and the one beside it."""
    out, pending, top = {}, [], None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            pending.append(stripped.lstrip("#").strip())
            continue
        m = re.match(r"^( {2})?([A-Za-z_]\w*):(.*)$", line)
        if m:
            top = top if m.group(1) else m.group(2)
            beside = re.search(r"\s#\s*(.*)$", m.group(3))
            out[f"{top}.{m.group(2)}" if m.group(1) else top] = " ".join(pending + ([beside.group(1)] if beside else [])).strip()
        pending = []
    return out


def sync_defaults(con):
    """Copy config.yaml into `settings`: a setting not there yet is added with the file's value, and every
    default and explanation is refreshed. A value already in the table is never touched."""
    from . import db
    notes = _comments(MASTER_FILE.read_text(encoding="utf-8"))
    for key, value in _leaves(file_config()):
        as_json = json.dumps(value, ensure_ascii=False)
        # settings is a view over the account's rows (db.py), which takes no ON CONFLICT: update, else add
        if not con.execute("UPDATE settings SET default_value=?, description=? WHERE key=?", (as_json, notes.get(key, ""), key)).rowcount:
            con.execute("INSERT INTO settings(key, value, default_value, description, changed_at) VALUES(?,?,?,?,?)",
                        (key, as_json, as_json, notes.get(key, ""), db.now()))
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('config_file', ?)", (_stamp(),))


def _refresh(con):
    """Pick up settings added to config.yaml since it was last read (an upgrade adds new ones there)."""
    seen = con.execute("SELECT value FROM meta WHERE key='config_file'").fetchone()
    if not seen or seen[0] != _stamp():
        sync_defaults(con)
        con.commit()


def _rows(con):
    _refresh(con)
    return con.execute("SELECT * FROM settings ORDER BY key").fetchall()


def load_config():
    """Every setting from the `settings` table, as the nested dict the bot works with."""
    from . import db
    con = db.connect()
    try:
        cfg = {}
        for r in _rows(con):
            section, _, name = r["key"].partition(".")
            if name:
                cfg.setdefault(section, {})[name] = json.loads(r["value"])
            else:
                cfg[section] = json.loads(r["value"])
        return cfg
    finally:
        con.close()


def _changed(r):
    return r["default_value"] is None or json.loads(r["value"]) != json.loads(r["default_value"])


def overrides():
    """Settings whose value differs from the default in config.yaml, as {key: value}."""
    from . import db
    con = db.connect()
    try:
        return {r["key"]: json.loads(r["value"]) for r in _rows(con) if _changed(r)}
    finally:
        con.close()


def settings_list():
    """One dict per setting: key, value, default, explanation, whether it was changed, and when."""
    from . import db
    con = db.connect()
    try:
        return [{"key": r["key"], "value": json.loads(r["value"]),
                 "default": json.loads(r["default_value"]) if r["default_value"] is not None else None,
                 "description": r["description"] or (PROCESSES.get(r["key"][8:], "") if r["key"].startswith("process.") else ""),
                 "changed": _changed(r), "changed_at": r["changed_at"]} for r in _rows(con)]
    finally:
        con.close()


KINDS = {bool: "true or false", int: "a number", float: "a number", str: "text", list: "a list", dict: "a table"}


def setting_problem(key, value):
    """Why this value cannot be saved for this setting, or None when it can."""
    if key.startswith("process."):
        return None if key[8:] in PROCESSES and isinstance(value, bool) else f"no such step, or not true/false: {key}"
    from . import db
    con = db.connect()
    try:
        row = con.execute("SELECT default_value FROM settings WHERE key=?", (key,)).fetchone()
    finally:
        con.close()
    if not row:
        return f"no such setting: {key}"
    default = json.loads(row["default_value"]) if row["default_value"] is not None else None
    if type(value) not in KINDS:
        return f"{key} cannot be empty"
    if default is not None and KINDS[type(value)] != KINDS[type(default)]:
        return f"{key} must be {KINDS[type(default)]}"
    return None


def set_setting(key, value):
    from . import db
    con = db.connect()
    as_json = json.dumps(value, ensure_ascii=False)
    if not con.execute("UPDATE settings SET value=?, changed_at=? WHERE key=?", (as_json, db.now(), key)).rowcount:
        con.execute("INSERT INTO settings(key, value, changed_at) VALUES(?,?,?)", (key, as_json, db.now()))
    con.commit(); con.close()


def reset_setting(key):
    """Back to the default from config.yaml. A setting with no default (a step switch) is removed. Returns 1 if it existed."""
    from . import db
    con = db.connect()
    n = con.execute("UPDATE settings SET value=default_value, changed_at=? WHERE key=? AND default_value IS NOT NULL",
                    (db.now(), key)).rowcount or con.execute("DELETE FROM settings WHERE key=?", (key,)).rowcount
    con.commit(); con.close()
    return n


def enabled(cfg, process):
    return bool((cfg.get("process") or {}).get(process, True))


def load_sources():
    """Every job source, from the `sources` table."""
    from . import sourcestore
    return sourcestore.load()


def set_source_enabled(name, enabled):
    from . import sourcestore
    return sourcestore.set_enabled(name, enabled)
