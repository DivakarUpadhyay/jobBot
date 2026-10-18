from . import alerts, generic, more_apis, notices, remote_apis, tenders, tw104

# adapter name (column `adapter` of the sources table) -> fetch(cfg, params) returning a list of Job
ADAPTERS = {
    "tw104": tw104.fetch,
    "tw104_companies": tw104.companies,   # openings at the companies in the `companies` table, in rotation
    "remotive": remote_apis.remotive,
    "remoteok": remote_apis.remoteok,
    "arbeitnow": remote_apis.arbeitnow,
    "wwr": remote_apis.wwr,
    "hn": remote_apis.hn,
    "jobicy": more_apis.jobicy,
    "himalayas": more_apis.himalayas,
    "workingnomads": more_apis.workingnomads,
    "themuse": more_apis.themuse,
    "yourator": more_apis.yourator,
    "freelancer": more_apis.freelancer,
    "jobrapido": more_apis.jobrapido,
    "instahyre": more_apis.instahyre,     # India: public job-search API
    "ted": tenders.ted,
    "ted_awards": tenders.ted_awards,          # companies that just won IT contracts (subcontracting leads)
    "uk_contracts": tenders.uk_contracts,
    "uk_find_a_tender": tenders.uk_find_a_tender,         # leads generated directly from official EU tenders
    "generic": generic.fetch,   # every source added by `source add` or discovery
    "notices": notices.fetch,   # recruitment notice boards (government bodies, institutes): PDFs and pages
    "email_alerts": alerts.fetch,   # job-alert emails in your own inboxes (LinkedIn, Indeed, Naukri, 1111 ...)
}
