# The Chief's Court Online Platform

## Run it
    pip install -r requirements.txt
    python app.py
Open http://127.0.0.1:5000 and log in as **admin / admin123** (change this password in `db.py` before real use).

## First steps
1. Admin: add villages (Villages), then create the chief, clerk, assessor, accountant and messenger accounts (Manage users).
2. Applicant: "Create an applicant account" on the login page, then File a case.
3. Clerk: approve or deny, set a hearing date (choose Online for a video hearing), send notices or a summons.
4. Chief: read the case, join the hearing, record a decision. Assessor: read-only. Accountant: logs payments. Messenger: downloads summons.

## Video hearings
Choose "Online" when scheduling. The applicant, clerk, chief and assessor of that case see a "Join video hearing"
button (dashboard, Hearings page and case page). The room has video, mute/camera buttons, a people list, chat and the case summary.
* Browsers need **HTTPS** (or localhost) to allow camera and microphone. Use HTTPS when you put the site online.
* Works for about 6 people at once. Video goes directly between browsers.
* Some mobile networks block direct connections. If that happens, add a TURN server to `ICE_SERVERS` in `app.py`.

## Files
    app.py          routes and permissions
    db.py           database tables and helpers
    templates/      HTML pages (base.html = sidebar and top bar, hearing_room.html = video room)
    static/         style.css, app.js (menu, tabs), room.js (video room)
    uploads/        uploaded files (created automatically)
