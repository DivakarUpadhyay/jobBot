# JobPilot admin panel

The operator's panel (`index.html`) and the bot console of the owner's own account
(`console.html`). It is its own site, separate from the customer portal, and it
runs the scheduler that starts each customer's bot runs.

    start-admin-panel.bat              (or: python -m jobbot serve --site admin --port 8766)

and open http://127.0.0.1:8766/

Customer accounts cannot sign in here; customer sign-up and /portal/* are not served.
Keep this site private: on a server, put it behind its own domain and HTTPS, or a VPN.
