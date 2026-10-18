"""Decide for each scraped job: skip it, or give it a 0-100 match score."""
import datetime as dt
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
CJK_RE = re.compile(r"[一-鿿]")
PART_RE = re.compile(r"(?i)\bpart[- ]?time\b|\bcontract(or)?\b|\bfreelance\b|\bhourly\b|\btemporary\b|兼職|工讀")
FREELANCE_RE = re.compile(r"(?i)\bfreelanc|\bcontract(or)?\b|\bgig\b|接案|外包")
NOT_EMAIL = re.compile(r"(?i)\.(png|jpg|jpeg|gif|svg|webp)$|noreply|no-reply|example\.")


def has(term, text):
    """Whole-term match that also works for terms like 'c#' and '.net'."""
    return re.search(r"(?<![\w.#+])" + re.escape(term) + r"(?![\w#+])", text) is not None


def cjk_ratio(text):
    letters = [c for c in text if c.isalpha()]
    return len(CJK_RE.findall(text)) / len(letters) if letters else 0.0


def find_emails(text):
    seen = []
    # a literal "\n" glued in front of an address ("\njobs@...") is not part of the address
    for e in EMAIL_RE.findall(re.sub(r"\\[nrt]", " ", text or "")):
        e = e.strip(".").lower()
        if e not in seen and not NOT_EMAIL.search(e):
            seen.append(e)
    return seen


def dedupe_key(job):
    norm = lambda s: re.sub(r"[\W_]+", "", (s or "").lower())
    return f"{norm(job.company)}|{norm(job.title)}"


def categorize(title, text):
    """Field of work, used for email labels and reports."""
    t = lambda *words: any(has(w, title) for w in words)
    dotnet = ("c#", ".net", "asp.net", ".net core", "entity framework", "sql server", "mssql")
    if t("teacher", "tutor", "teaching", "instructor", "esl"):
        return "Teaching"
    if t("c#", ".net", "asp.net") or sum(has(s, text) for s in dotnet) >= 2:
        return "Software .NET"
    if t("data engineer", "sql", "dba", "etl", "database", "bi developer", "data analyst"):
        return "Data / SQL"
    if t("software engineer", "developer", "programmer", "backend", "back-end", "frontend", "front-end",
         "full stack", "full-stack", "fullstack", "devops", "architect"):
        return "Software other"
    if t("qa", "tester", "test engineer", "sdet", "support", "helpdesk", "customer success"):
        return "QA / Support"
    if t("engineer", "software") or any(w in title for w in ("工程師", "軟體", "程式", "開發", "後端", "前端")):
        return "Software other"
    # Non-technical office work done on a computer (user, 2026-10-15: "non technical but computer related ... office")
    if t("it admin", "it administrator", "system administrator", "sysadmin", "mis", "it assistant", "it specialist",
         "it coordinator", "network admin", "office assistant", "administrative assistant", "admin assistant",
         "administrative specialist", "office administrator", "office admin", "executive assistant", "secretary",
         "data entry", "document control", "document controller", "erp", "sap", "operations assistant",
         "e-commerce operations", "e-commerce specialist", "project coordinator", "project assistant", "pmo",
         "business analyst", "back office", "office coordinator", "data clerk") \
            or any(w in title for w in ("行政", "助理", "資訊人員", "網管", "文書", "資料輸入", "電商", "秘書", "系統管理")):
        return "Office / IT admin"
    return "Other"


def priority_country(job, cfg):
    """Name of the priority country the job is located in, or None."""
    places = (cfg.get("priority_countries") or {}).get("places") or {}
    if job.region == "taiwan" and "Taiwan" in places:
        return "Taiwan"
    place = (job.location or "").lower()
    return next((country for country, words in places.items() if any(has(str(w).lower(), place) for w in words)), None)


UNI_SOFTWARE = re.compile(r"(?i)software|developer|programmer|\bengineer\b.*(software|web|application|systems|data|cloud|devops)|"
                          r"research software|\brse\b|full[- ]?stack|back[- ]?end|front[- ]?end|\.net|c#|web (developer|engineer)|"
                          r"application (developer|analyst)|systems? (developer|analyst|programmer)|devops|data engineer|"
                          r"blockchain|database (developer|administrator)|\bdba\b")


ACADEMIC = re.compile(r"(?i)\bph\.?\s?d\b|doctoral|post-?doc|postdoctoral|\bdoctorate\b|research fellow|\bfellowship\b|"
                      r"professor|\bfaculty\b|\blecturer\b|tenure|\bdoktorand|\bpromotion\b.*stelle|博士")


# Words that say how senior a role is, not what it is: "Senior Data Analyst" is a data analyst.
LEVEL_WORDS = {"senior", "sr", "junior", "jr", "lead", "principal", "head", "chief", "associate", "assistant", "entry", "level",
               "trainee", "graduate", "fresher", "i", "ii", "iii", "iv", "1", "2", "3", "the", "and", "of", "for", "in", "at", "a", "an",
               "with", "to", "or", "remote", "hybrid", "onsite", "on-site", "full", "part", "time", "contract", "temporary", "m", "f", "d"}


def _tokens(text):
    """Words of a title, lower case, a plural 's' dropped ('nurses' -> 'nurse'), so titles compare word by word."""
    words = re.findall(r"[a-z0-9][a-z0-9+#.]*", (text or "").lower())
    return {w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w for w in words}


def title_fit(title, targets):
    """How the job title meets the roles the candidate aims for:
    'role'  every meaningful word of one target role is in the title ("ICU Nurse" ~ "Staff Nurse - ICU"),
    'word'  only the role's main word is ("Nurse Educator" for "ICU Nurse"),
    None    neither: a different job."""
    have = _tokens(title)
    heads = set()
    for t in targets:
        words = [w for w in _tokens(t) if w not in LEVEL_WORDS]
        if words and all(w in have for w in words):
            return "role"
        main = [w for w in re.findall(r"[a-z0-9][a-z0-9+#.]*", t.lower()) if w not in LEVEL_WORDS]
        if main:
            heads.add(next(iter(_tokens(main[-1]))))   # the last word names the job: analyst, nurse, developer
    return "word" if heads & have else None


JUNIOR = re.compile(r"(?i)\b(junior|jr|entry|fresher|trainee|graduate|intern)\b")
SENIOR = re.compile(r"(?i)\b(senior|sr|principal|lead|head|director|chief|vp|manager|architect)\b|\bstaff\s+(software|engineer|data)")


def judge(job, cfg, has_email=False):
    """Returns dict(skip, score, notes, emails, dedupe_key, track, category). Also fills job.job_type if empty.
    has_email: an HR address is already known for this job (found outside the description)."""
    title, desc = job.title.lower(), (job.description or "").lower()
    text = f"{title}\n{desc}"
    prof, avoid = cfg["profile"], cfg["avoid"]
    if not job.job_type:
        job.job_type = "part" if PART_RE.search(title) or PART_RE.search(desc[:400]) else "full"
    track = job.track or job.job_type
    if FREELANCE_RE.search(title) or (job.job_type == "part" and FREELANCE_RE.search(desc[:400])):
        track, job.job_type = "freelance", "part"

    skip = None
    strict = bool((cfg.get("match") or {}).get("strict"))   # customer accounts: only jobs that fit the candidate's own roles
    if strict:   # customers type roles and skills in any case ("SQL", "Power BI"); the text compared is lower case
        prof = {k: [str(x).lower() for x in v] if isinstance(v, list) else v for k, v in prof.items()}
    # Never academic research posts (your rule: you are a software developer, not a PhD candidate or researcher)
    if strict:
        pass   # the two owner rules below describe one software developer; customers are judged by their own roles
    elif ACADEMIC.search(title):
        skip = "PhD / postdoc / faculty position: you apply as a software developer only"
    # University job boards: software positions only (your rule), whatever else the profile accepts
    elif "universit" in (job.source or "").lower() and not UNI_SOFTWARE.search(title):
        skip = "university job that is not a software position"
    regions = [r.lower() for r in cfg["regions"]]
    place = (job.location or "").lower()
    excluded = next((country for country, names in (cfg.get("exclude_countries") or {}).items()
                     if any(has(n.lower(), place) for n in names)), None)
    wanted_place = any(r != "remote" and r in (job.location or "").lower() for r in regions)
    remote_only = next((country for country, names in (cfg.get("remote_only_countries") or {}).items()
                        if any(has(n.lower(), place) for n in names)), None)
    tw = cfg.get("taiwan") or {}
    category = categorize(title, text)
    has_email = has_email or bool(find_emails(job.description))
    if excluded:
        skip = f"country excluded: {excluded}"
    elif remote_only and not job.remote and track != "freelance":
        skip = f"{remote_only}: only remote or freelance work wanted"
    elif job.region != "taiwan" and not (job.remote and "remote" in regions) and not wanted_place \
            and not cfg.get("open_to_relocation"):
        skip = "on-site outside your regions"
    elif job.region == "taiwan" and "taiwan" not in regions:
        skip = "Taiwan not in your regions"
    elif any(has(w, title) for w in avoid["title_words"]):
        skip = "title on avoid list"
    elif any(p.lower() in text for p in avoid["mandarin_phrases"]):
        skip = "Mandarin required"
    elif max(cjk_ratio(job.title), cjk_ratio(f"{job.title}\n{job.description}")) > avoid["max_cjk_ratio"] \
            and not (job.region == "taiwan" and tw.get("chinese_posts_with_email") and has_email):
        skip = "post mostly in Chinese"
    elif job.region == "taiwan" and tw.get("non_technical_part_time_only") and job.job_type == "full" \
            and category not in (tw.get("technical_categories") or []):
        skip = "Taiwan: non-technical full-time (only part-time or temporary wanted)"
    elif job.posted_at:
        try:
            if (dt.date.today() - dt.date.fromisoformat(job.posted_at)).days > cfg["max_age_days"]:
                skip = "too old"
        except ValueError:
            pass

    score, notes = 0, []
    fit = title_fit(title, prof["titles"] + prof["titles_secondary"]) if strict else None
    if any(has(t, title) for t in prof["titles"]) or fit == "role":
        score += 30; notes.append("title")
    elif any(has(t, title) for t in prof["titles_secondary"]):
        score += 25; notes.append("title2")
    elif fit == "word":
        score += 15; notes.append("role word")
    in_title = [s for s in prof["skills_primary"] if has(s, title)]
    in_desc = [s for s in prof["skills_primary"] if s not in in_title and has(s, desc)]
    score += min(45, 15 * len(in_title) + 8 * len(in_desc))
    secondary = [s for s in prof["skills_secondary"] if has(s, text)]
    score += min(15, 3 * len(secondary))
    if in_title or in_desc or secondary:
        notes.append("skills: " + ", ".join((in_title + in_desc + secondary)[:6]))
    if job.track == "freelance" and (in_title or in_desc):
        score += 15; notes.append("project in your stack")  # project titles rarely name a role
    wanted = priority_country(job, cfg)
    if wanted:
        score += (cfg.get("priority_countries") or {}).get("bonus", 15); notes.append(f"priority country: {wanted}")
    if job.region == "taiwan":
        score += 10; notes.append("local")
    elif job.location and not re.search(cfg["remote_open_pattern"], job.location, re.I):
        score -= cfg["remote_restricted_penalty"]; notes.append(f"location-restricted ({job.location[:30]})")

    if strict and not skip:
        # Relevance comes first: a job is kept only when its title is one of the candidate's roles. Skills found
        # somewhere in a description, or a favoured country, never make a different job relevant.
        skills_hit = len(in_title) + len(in_desc)
        if not prof["titles"] + prof["titles_secondary"]:
            skip = "not relevant: no target roles are set in your profile"
        elif track == "freelance" and not fit:
            if not in_title or skills_hit < 2:   # a project without a role title must name one of your skills in its title
                skip = "not relevant: the project does not name your role, or your skills in its title"
        elif not fit:
            skip = "not relevant: the job title is not one of your target roles"
        elif JUNIOR.search(" ".join(prof["titles"])) and not JUNIOR.search(title) and SENIOR.search(title):
            skip = "not relevant: a senior role, and you aim for junior or entry-level roles"
        elif fit == "word" and skills_hit < (1 if not prof["skills_primary"] else 2):
            skip = "not relevant: only the role's main word matches and too few of your skills are asked for"

    return {"skip": skip, "score": max(0, min(100, score)), "notes": "; ".join(notes),
            "emails": find_emails(job.description), "dedupe_key": dedupe_key(job),
            "track": track, "category": category}
