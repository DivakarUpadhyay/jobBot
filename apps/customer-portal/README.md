# JobPilot customer portal

The public website and the customer app, as one single page (`index.html`):
About, Product, Plans, Contact, Register and Sign in, then first-time set-up
(details, resume, cover letter, jobs to apply for), then the dashboard.

It is its own site, separate from the admin panel. Start it with:

    start-customer-portal.bat          (or: python -m jobbot serve --site customer --port 8765)

and open http://127.0.0.1:8765/

The page talks only to these API routes: /login, /logout, /me, /plans, /register,
/enquiries, /portal/* and /schedules. Every other route answers 404 on this site.

Access rules (enforced by the server, not by the page):
- Registering and the first set-up are free.
- After set-up, the profile, resume, cover letter and job preferences are read-only
  until a payment succeeds; the bot, mailbox and AI resume studio need a paid plan.
- The AI resume studio only works on the customer's resume and cover letter.
