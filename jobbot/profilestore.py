"""Your resume data, the email wording and the resume layout, kept in the database table `documents`.

The files in profile/ are read once, when the table is first created, and never again. Change a
document with `PUT /profile/{name}`; a change that would break resume or email writing is refused."""
import json
from contextlib import contextmanager

import yaml

from . import db, starting

# name -> (starting file, how it is stored)
DOCUMENTS = {
    "resume": ("master_resume.yaml", "json"),             # every fact a resume may use
    "email-templates": ("email_templates.yaml", "json"),  # wording per kind of email
    "resume-template": ("resume_template.html", "text"),  # the resume layout (HTML with Jinja fields)
}


def create(con):
    """Create and fill `documents` (migration step; the caller commits)."""
    # documents is one of the account's views in the single database (db.py): only the rows are added here
    for name, (file, kind) in DOCUMENTS.items():
        path = starting("profile", file)
        if path.exists():
            text = path.read_text(encoding="utf-8")
            content = text if kind == "text" else json.dumps(yaml.safe_load(text), ensure_ascii=False)
            con.execute("INSERT OR IGNORE INTO documents(name, format, content, changed_at) VALUES(?,?,?,?)",
                        (name, kind, content, db.now()))


@contextmanager
def _db(con):
    own = con is None
    con = con or db.connect()
    try:
        yield con
    finally:
        if own:
            con.close()


def get(name, con=None):
    """The document: a dict for `resume` and `email-templates`, the HTML text for `resume-template`."""
    with _db(con) as c:
        r = c.execute("SELECT format, content FROM documents WHERE name=?", (name,)).fetchone()
        if not r:
            raise KeyError(f"no document named {name}")
        return r["content"] if r["format"] == "text" else json.loads(r["content"])


def changed_at(name, con=None):
    with _db(con) as c:
        r = c.execute("SELECT changed_at FROM documents WHERE name=?", (name,)).fetchone()
        return r["changed_at"] if r else None


def problem(name, value):
    """Why this version cannot be saved, or None when it can. It must still produce a resume."""
    from . import render
    if name not in DOCUMENTS:
        return f"no such document: {name}. Documents: {', '.join(DOCUMENTS)}"
    if name == "resume-template":
        if not isinstance(value, str) or not value.strip():
            return "the template must be text"
        resume, template = get("resume"), value
    else:
        old = get(name)
        if not isinstance(value, dict):
            return "the document must be an object"
        missing = [k for k in old if k not in value]
        if missing:
            return "these parts are missing: " + ", ".join(missing)
        wrong = [k for k in old if type(value[k]) is not type(old[k])]
        if wrong:
            return "these parts have the wrong kind of value: " + ", ".join(wrong)
        if name == "email-templates":
            return None
        resume, template = value, get("resume-template")
    try:
        for region in ("taiwan", "abroad"):
            render.check_region_rule(render.to_html(render.build(region, master=resume), template), region)
    except Exception as e:
        return f"a resume cannot be built from this: {type(e).__name__}: {e}"
    return None


def put(name, value, con=None):
    """Save a new version. Raises ValueError with the reason when it is refused."""
    reason = problem(name, value)
    if reason:
        raise ValueError(reason)
    with _db(con) as c:
        content = value if DOCUMENTS[name][1] == "text" else json.dumps(value, ensure_ascii=False)
        c.execute("UPDATE documents SET content=?, changed_at=? WHERE name=?", (content, db.now(), name))
        c.commit()
