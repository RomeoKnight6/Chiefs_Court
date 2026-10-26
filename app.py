"""
The Chief's Court Online Platform
---------------------------------
Roles:  admin, chief, clerk, assessor, accountant, messenger (officials) and applicant (public).
Run:    python app.py     then open http://127.0.0.1:5000   (first login: admin / admin123)
"""
import json
import os
import uuid
from datetime import datetime
from functools import wraps

from flask import (Flask, current_app,abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import db

app = Flask(__name__)
app.secret_key = "change-this-secret-key-before-going-live"
app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "uploads")
app.teardown_appcontext(db.close_db)

# Video hearings connect browsers directly to each other. STUN servers help them find each other.
# For people behind strict mobile networks add a TURN server here (e.g. from a provider such as Metered or coturn).
app.config["ICE_SERVERS"] = [{"urls": "stun:stun.l.google.com:19302"}]

OFFICIAL_ROLES = ["chief", "clerk", "assessor", "accountant", "messenger"]
CASE_STATUSES = ["Approved", "Scheduled", "In Hearing", "Adjourned", "Decided", "Closed"]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_upload(file):
    """Save an uploaded file with a unique name. Returns (original_name, stored_name)."""
    original = secure_filename(file.filename)
    stored = uuid.uuid4().hex + "_" + original
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], stored))
    return original, stored


def notify(user_id, subject, message="", case_id=None):
    """Send an in-app notification to one user."""
    db.execute(
        "INSERT INTO notifications (user_id, subject, message, case_id, created_at) VALUES (?,?,?,?,?)",
        (user_id, subject, message, case_id, now()),
    )


def notify_officials(jurisdiction, roles, subject, message, case_id):
    """Send a notification to every active official with one of `roles` in a jurisdiction."""
    marks = ",".join("?" * len(roles))
    officials = db.query(
        f"SELECT id FROM users WHERE jurisdiction = ? AND active = 1 AND role IN ({marks})",
        (jurisdiction, *roles),
    )
    for official in officials:
        notify(official["id"], subject, message, case_id)


# ---------------------------------------------------------------------------
# Login handling and access control
# ---------------------------------------------------------------------------
@app.before_request
def load_current_user():
    user_id = session.get("user_id")
    g.user = db.query("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,), one=True) if user_id else None


@app.context_processor
def inject_globals():
    """Values every template can use (e.g. the unread count on the bell icon)."""
    unread = 0
    if g.user:
        unread = db.query("SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND is_read = 0",
                          (g.user["id"],), one=True)["n"]
    return {"user": g.user, "unread_count": unread, "case_statuses": CASE_STATUSES, "nav_counts": nav_counts()}


def nav_counts():
    """Small numbers shown next to sidebar links."""
    counts = {"review": 0, "summons": 0}
    if g.user and g.user["role"] == "clerk":
        counts["review"] = db.query("SELECT COUNT(*) AS n FROM cases WHERE jurisdiction = ? AND status = 'Pending'",
                                    (g.user["jurisdiction"],), one=True)["n"]
    if g.user and g.user["role"] == "messenger":
        counts["summons"] = db.query("SELECT COUNT(*) AS n FROM summons WHERE messenger_id = ? AND status = 'Pending'",
                                     (g.user["id"],), one=True)["n"]
    return counts


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def role_required(*roles):
    """Only let the listed roles open a page."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            if g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator



def can_view_case(case):
    role = g.user["role"]

    if role == "applicant":
        if case["applicant_id"] == g.user["id"]:
            return True

        # Allow access if a clerk has approved the applicant's request.
        approved_request = db.query(
            """
            SELECT id
            FROM case_access_requests
            WHERE case_id = ?
              AND respondent_id = ?
              AND status = 'Approved'
            """,
            (case["id"], g.user["id"]),
            one=True
        )

        return approved_request is not None

    if role in ("admin", "messenger"):
        return False

    if case["jurisdiction"] != g.user["jurisdiction"]:
        return False

    if role in ("chief", "assessor"):
        # They only see cases the clerk has approved.
        return case["status"] not in ("Pending", "Denied")

    return True
                      # clerk and accountant


def get_case_or_403(case_id):
    case = db.query("SELECT * FROM cases WHERE id = ?", (case_id,), one=True)
    if case is None:
        abort(404)
    if not can_view_case(case):
        abort(403)
    return case



def cases_for_current_user():
    """Return the cases the logged-in user is allowed to see."""
    role = g.user["role"]

    if role == "applicant":
        return db.query(
            """
            SELECT DISTINCT c.*
            FROM cases AS c
            LEFT JOIN case_access_requests AS r
                ON r.case_id = c.id
                AND r.respondent_id = ?
                AND r.status = 'Approved'
            WHERE c.applicant_id = ?
               OR r.id IS NOT NULL
            ORDER BY c.id DESC
            """,
            (g.user["id"], g.user["id"])
        )

    if role in ("chief", "assessor"):
        return db.query(
            """
            SELECT *
            FROM cases
            WHERE jurisdiction = ?
              AND status NOT IN ('Pending', 'Denied')
            ORDER BY id DESC
            """,
            (g.user["jurisdiction"],)
        )

    return db.query(
        """
        SELECT *
        FROM cases
        WHERE jurisdiction = ?
        ORDER BY id DESC
        """,
        (g.user["jurisdiction"],)
    )


def hearings_for_current_user():
    """Return upcoming hearings for cases the current user can access."""
    today = datetime.now().strftime("%Y-%m-%d")

    base = """
        SELECT DISTINCT
            h.*,
            c.case_number,
            c.cause_of_action,
            c.jurisdiction
        FROM hearings h
        JOIN cases c ON c.id = h.case_id
        WHERE h.hearing_date >= ?
    """

    if g.user["role"] == "applicant":
        return db.query(
            base + """
                AND (
                    c.applicant_id = ?
                    OR EXISTS (
                        SELECT 1
                        FROM case_access_requests car
                        WHERE car.case_id = c.id
                          AND car.respondent_id = ?
                          AND car.status = 'Approved'
                    )
                )
                ORDER BY h.hearing_date, h.hearing_time
            """,
            (today, g.user["id"], g.user["id"])
        )

    return db.query(
        base + """
            AND c.jurisdiction = ?
            ORDER BY h.hearing_date, h.hearing_time
        """,
        (today, g.user["jurisdiction"])
    )


# ---------------------------------------------------------------------------
# Sign in, register, public access
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    return redirect(url_for("dashboard") if g.user else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        found = db.query("SELECT * FROM users WHERE username = ?", (request.form["username"].strip(),), one=True)
        if found is None or not check_password_hash(found["password_hash"], request.form["password"]):
            flash("Wrong username or password.", "error")
        elif not found["active"]:
            flash("This account has been disabled. Contact the system administrator.", "error")
        else:
            session.clear()
            session["user_id"] = found["id"]
            return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    """Applicants create their own account. Officials are created by the admin."""
    if request.method == "POST":
        form = request.form
        if form["password"] != form["confirm"]:
            flash("Passwords do not match.", "error")
        elif db.query("SELECT 1 FROM users WHERE username = ?", (form["username"].strip(),), one=True):
            flash("That username is already taken.", "error")
        else:
            db.execute(
                "INSERT INTO users (username, password_hash, first_name, surname, email, phone, role) "
                "VALUES (?,?,?,?,?,?, 'applicant')",
                (form["username"].strip(), generate_password_hash(form["password"]), form["first_name"].strip(),
                 form["surname"].strip(), form["email"].strip(), form["phone"].strip()),
            )
            flash("Account created. You can now log in.", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/public", methods=["GET", "POST"])
def public_access():
    """Anyone can check a case number to verify its status and order."""
    case = None
    searched = False
    if request.method == "POST":
        searched = True
        case = db.query("SELECT * FROM cases WHERE case_number = ?",
                        (request.form["case_number"].strip().upper(),), one=True)
    return render_template("public.html", case=case, searched=searched)


# ---------------------------------------------------------------------------
# Dashboard ("Pending My Action")
# ---------------------------------------------------------------------------
def greeting():
    hour = datetime.now().hour
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"


def count_status(cases, statuses):
    return len([c for c in cases if c["status"] in statuses])


@app.route("/dashboard")
@login_required
def dashboard():
    role = g.user["role"]
    context = {"pending_cases": [], "hearings": [], "summons": [], "stats": [], "greeting": greeting()}

    if role == "admin":
        context["stats"] = [
            ("Officials", db.query("SELECT COUNT(*) n FROM users WHERE role NOT IN ('admin','applicant')", one=True)["n"]),
            ("Applicants", db.query("SELECT COUNT(*) n FROM users WHERE role = 'applicant'", one=True)["n"]),
            ("Cases filed", db.query("SELECT COUNT(*) n FROM cases", one=True)["n"]),
            ("Villages", db.query("SELECT COUNT(*) n FROM jurisdictions", one=True)["n"]),
        ]
    elif role == "messenger":
        rows = db.query("SELECT s.*, c.case_number FROM summons s JOIN cases c ON c.id = s.case_id "
                        "WHERE s.messenger_id = ? ORDER BY s.id DESC", (g.user["id"],))
        context["summons"] = [s for s in rows if s["status"] == "Pending"]
        context["stats"] = [("To serve", len(context["summons"])), ("Served", len(rows) - len(context["summons"]))]
    else:
        cases = cases_for_current_user()
        hearings = hearings_for_current_user()
        context["hearings"] = hearings

        if role == "clerk":        # new cases to review, approved cases that still need a date
            context["pending_cases"] = [c for c in cases if c["status"] in ("Pending", "Approved")]
            context["stats"] = [("Awaiting review", count_status(cases, ["Pending"])),
                                ("Need a hearing date", count_status(cases, ["Approved"])),
                                ("Upcoming hearings", len(hearings)), ("All cases", len(cases))]
        elif role == "chief":      # heard cases waiting for a decision
            context["pending_cases"] = [c for c in cases if c["status"] in ("Scheduled", "In Hearing", "Adjourned")]
            context["stats"] = [("Awaiting decision", len(context["pending_cases"])),
                                ("Upcoming hearings", len(hearings)),
                                ("Decided", count_status(cases, ["Decided", "Closed"]))]
        elif role == "applicant":
            context["pending_cases"] = [c for c in cases if c["status"] not in ("Decided", "Closed", "Denied")]
            unread = db.query("SELECT COUNT(*) n FROM notifications WHERE user_id = ? AND is_read = 0", (g.user["id"],), one=True)["n"]
            context["stats"] = [("Open cases", len(context["pending_cases"])), ("Upcoming hearings", len(hearings)),
                                ("Unread notices", unread)]
        elif role == "assessor":
            context["pending_cases"] = cases[:10]
            context["stats"] = [("Open cases", count_status(cases, ["Approved", "Scheduled", "In Hearing", "Adjourned"])),
                                ("Upcoming hearings", len(hearings)), ("All cases", len(cases))]
        else:                      # accountant
            context["pending_cases"] = cases[:10]
            total = db.query("SELECT COUNT(*) n, COALESCE(SUM(p.amount), 0) total FROM payments p "
                             "JOIN cases c ON c.id = p.case_id WHERE c.jurisdiction = ?", (g.user["jurisdiction"],), one=True)
            context["stats"] = [("Payments logged", total["n"]), ("Total collected", "{:,.2f}".format(total["total"])),
                                ("Cases", len(cases))]

    return render_template("dashboard.html", **context)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
@app.route("/cases")
@role_required("applicant", "clerk", "chief", "assessor", "accountant")
def case_list():
    cases = cases_for_current_user()
    search = request.args.get("q", "").strip().lower()
    if search:
        cases = [c for c in cases if search in " ".join(
            [c["case_number"], c["applicant_name"], c["applicant_surname"], c["cause_of_action"]]).lower()]
    return render_template("case_list.html", cases=cases, search=search)


@app.route("/cases/new", methods=["GET", "POST"])
@role_required("applicant")
def case_new():
    jurisdictions = db.query("SELECT name FROM jurisdictions ORDER BY name")
    if request.method == "POST":
        form = request.form
        number = "CC-{}-{:04d}".format(datetime.now().year, db.query("SELECT COUNT(*) n FROM cases", one=True)["n"] + 1)

        case_id = db.execute(
    "INSERT INTO cases (case_number, applicant_id, applicant_name, "
    "applicant_surname, jurisdiction, participants, cause_of_action, "
    "description, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
    (
        number,
        g.user["id"],
        form["applicant_name"].strip(),
        form["applicant_surname"].strip(),
        form["jurisdiction"],
        "",  # Individual participants are saved in case_participants
        form["cause_of_action"].strip(),
        form.get("description", "").strip(),
        now(),
    ),
)
        # Get the separate participant fields submitted by the form.
        roles = form.getlist("participant_role")
        first_names = form.getlist("participant_name")
        surnames = form.getlist("participant_surname")

        # Save each respondent or witness against the new case.
        for role, first_name, surname in zip(roles, first_names, surnames):
            first_name = first_name.strip()
            surname = surname.strip()

            # Ignore completely empty rows, but reject incomplete rows.
            if not first_name and not surname:
                continue

            if role not in ("Respondent", "Witness") or not first_name or not surname:
                flash(
                    "Each participant must have a valid role, name and surname.",
                    "error",
                )
                # Ideally validate participants BEFORE inserting the case.
                # See the validation note below.
                return redirect(url_for("case_new"))

            db.execute(
                "INSERT INTO case_participants "
                "(case_id, role, first_name, surname) VALUES (?,?,?,?)",
                (case_id, role, first_name, surname),
            )


        for file in request.files.getlist("files"):
            if file.filename:
                original, stored = save_upload(file)
                db.execute("INSERT INTO case_files (case_id, filename, stored_name, created_at) VALUES (?,?,?,?)",
                           (case_id, original, stored, now()))

        notify(g.user["id"], "Case filed", f"Your case {number} was sent to the clerk for review.", case_id)
        notify_officials(form["jurisdiction"], ["clerk"], "New case to review",
                         f"Case {number} ({form['cause_of_action']}) is waiting for your review.", case_id)
        flash(f"Case {number} was filed. The clerk will review it.", "success")
        return redirect(url_for("case_detail", case_id=case_id))
    return render_template("case_form.html", jurisdictions=jurisdictions)


def review_case_access(request_id, decision):
    if decision not in ("Approved", "Rejected"):
        abort(400)

    access_request = db.query(
        """
        SELECT r.id, r.case_id, r.respondent_id, r.status,
               c.case_number, c.jurisdiction
        FROM case_access_requests AS r
        JOIN cases AS c ON c.id = r.case_id
        WHERE r.id = ?
        """,
        (request_id,),
        one=True
    )

    if access_request is None:
        abort(404)

    # Only a clerk in the case's jurisdiction can review the request.
    if access_request["jurisdiction"] != g.user["jurisdiction"]:
        abort(403)

    if access_request["status"] != "Pending":
        flash("This request has already been reviewed.", "error")
        return redirect(
            url_for("case_detail", case_id=access_request["case_id"])
        )

    db.execute(
        """
        UPDATE case_access_requests
        SET status = ?,
            reviewed_by = ?,
            reviewed_at = ?
        WHERE id = ?
          AND status = 'Pending'
        """,
        (decision, g.user["id"], now(), request_id)
    )

    if decision == "Approved":
        notify(
            access_request["respondent_id"],
            "Case access approved",
            f"Your request to access case "
            f"{access_request['case_number']} has been approved.",
            access_request["case_id"]
        )
        flash("Case access request approved.", "success")
    else:
        notify(
            access_request["respondent_id"],
            "Case access request rejected",
            f"Your request to access case "
            f"{access_request['case_number']} has been rejected.",
            access_request["case_id"]
        )
        flash("Case access request rejected.", "success")

    return redirect(
        url_for("case_detail", case_id=access_request["case_id"])
    )



@app.route("/cases/<int:case_id>")
@login_required
def case_detail(case_id):
    case = get_case_or_403(case_id)
    role = g.user["role"]
    data = {
        "case": case,
        "files": db.query("SELECT * FROM case_files WHERE case_id = ?", (case_id,)),
        "hearings": db.query("SELECT * FROM hearings WHERE case_id = ? ORDER BY hearing_date, hearing_time", (case_id,)),
        "payments": [],
        "summons_list": [],
        "messengers": [],
        "access_requests":[],
        "submitted_payments":[],
        "participants": db.query(
    "SELECT * FROM case_participants WHERE case_id = ? ORDER BY id",
    (case_id,),
),
    }

    if role in ("accountant", "clerk"):
        data["payments"] = db.query("SELECT * FROM payments WHERE case_id = ? ORDER BY id DESC", (case_id,))
        data["submitted_payments"]=db.query("""
    SELECT sp.*, c.case_number
    FROM submitted_payments AS sp
    JOIN cases AS c ON c.id = sp.case_id
    WHERE sp.case_id = ? AND sp.status = 'Pending'
    ORDER BY sp.created_at DESC, sp.id DESC
""", (case_id,))
        
    if role in ["applicant"]:
        data["payments"] = db.query("SELECT * FROM payments WHERE case_id = ? ORDER BY id DESC", (case_id,))
        
    if role == "clerk":
        data["summons_list"] = db.query(
            "SELECT s.*, u.first_name || ' ' || u.surname AS messenger_name FROM summons s "
            "JOIN users u ON u.id = s.messenger_id WHERE s.case_id = ? ORDER BY s.id DESC", (case_id,))
        data["messengers"] = db.query("SELECT * FROM users WHERE role = 'messenger' AND active = 1 AND jurisdiction = ?",
                                      (case["jurisdiction"],))
        data["access_requests"] = db.query(
        """
        SELECT
            car.id,
            car.status,
            car.created_at,
            car.respondent_id,
            u.first_name || ' ' || u.surname AS respondent_name,
            u.email AS respondent_email,
            c.case_number
        FROM case_access_requests car
        JOIN users u ON u.id = car.respondent_id
        JOIN cases c ON c.id = car.case_id
        WHERE car.case_id = ?
        ORDER BY car.created_at DESC
        """,
        (case_id,)
    )
    print("ACCESS REQUESTS:", [dict(row) for row in data["access_requests"]])
    return render_template("case_detail.html", **data)


# --- clerk actions ---------------------------------------------------------
@app.route("/cases/<int:case_id>/review", methods=["POST"])
@role_required("clerk")
def case_review(case_id):
    case = get_case_or_403(case_id)
    if request.form["decision"] == "approve":
        db.execute("UPDATE cases SET status = 'Approved' WHERE id = ?", (case_id,))
        notify(case["applicant_id"], "Case approved", f"Case {case['case_number']} was approved. "
               "A hearing date will be set soon.", case_id)
        notify_officials(case["jurisdiction"], ["chief", "assessor"], "New case approved",
                         f"Case {case['case_number']} is now open.", case_id)
        flash("Case approved. You can now set a hearing date.", "success")
    else:
        reason = request.form.get("reason", "").strip() or "No reason given."
        db.execute("UPDATE cases SET status = 'Denied', denial_reason = ? WHERE id = ?", (reason, case_id))
        notify(case["applicant_id"], "Case denied", f"Case {case['case_number']} was denied. Reason: {reason}", case_id)
        flash("Case denied.", "success")
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/cases/<int:case_id>/schedule", methods=["POST"])
@role_required("clerk")
def case_schedule(case_id):
    case = get_case_or_403(case_id)
    form = request.form
    room_code = uuid.uuid4().hex[:12] if form["mode"] == "Online" else ""
    db.execute(
        "INSERT INTO hearings (case_id, hearing_date, hearing_time, mode, location, created_by, room_code) "
        "VALUES (?,?,?,?,?,?,?)",
        (case_id, form["hearing_date"], form["hearing_time"], form["mode"], form.get("location", "").strip(),
         g.user["id"], room_code),
    )
    db.execute("UPDATE cases SET status = 'Scheduled' WHERE id = ?", (case_id,))

    text = f"Hearing for case {case['case_number']} on {form['hearing_date']} at {form['hearing_time']} ({form['mode']})."
    if form["mode"] == "Online":
        text += " Join the video room from the Hearings page."
    notify(case["applicant_id"], "Hearing scheduled", text, case_id)
    notify_officials(case["jurisdiction"], ["chief", "assessor"], "Hearing scheduled", text, case_id)
    flash("Hearing scheduled and everyone has been notified.", "success")
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/cases/<int:case_id>/status", methods=["POST"])
@role_required("clerk")
def case_status(case_id):
    case = get_case_or_403(case_id)
    status = request.form["status"]
    if status in CASE_STATUSES:
        db.execute("UPDATE cases SET status = ? WHERE id = ?", (status, case_id))
        notify(case["applicant_id"], "Case status updated", f"Case {case['case_number']} is now: {status}.", case_id)
        flash("Status updated.", "success")
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/cases/<int:case_id>/notice", methods=["POST"])
@role_required("clerk")
def case_notice(case_id):
    case = get_case_or_403(case_id)
    notify(case["applicant_id"], request.form["subject"].strip(), request.form["message"].strip(), case_id)
    flash("Notice sent to the applicant.", "success")
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/cases/<int:case_id>/summons", methods=["POST"])
@role_required("clerk")
def case_summons(case_id):
    case = get_case_or_403(case_id)
    file = request.files.get("document")
    if not file or not file.filename:
        flash("Please attach the summons document.", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    original, stored = save_upload(file)
    db.execute(
        "INSERT INTO summons (case_id, messenger_id, recipient_name, notes, filename, stored_name, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (case_id, request.form["messenger_id"], request.form["recipient_name"].strip(),
         request.form["notes"].strip(), original, stored, now()),
    )
    notify(int(request.form["messenger_id"]), "New summons to deliver",
           f"Case {case['case_number']}: serve {request.form['recipient_name']}.", case_id)
    flash("Summons sent to the messenger.", "success")
    return redirect(url_for("case_detail", case_id=case_id))


# --- chief action ----------------------------------------------------------
@app.route("/cases/<int:case_id>/decision", methods=["POST"])
@role_required("chief")
def case_decision(case_id):
    case = get_case_or_403(case_id)
    decision = request.form["decision"].strip()
    if decision:
        db.execute("UPDATE cases SET decision = ?, decided_by = ?, decided_at = ?, status = 'Decided' WHERE id = ?",
                   (decision, g.user["id"], now(), case_id))
        notify(case["applicant_id"], "Decision made", f"The chief has made a decision on case {case['case_number']}.", case_id)
        notify_officials(case["jurisdiction"], ["clerk"], "Decision recorded",
                         f"The chief decided case {case['case_number']}.", case_id)
        flash("Decision saved.", "success")
    return redirect(url_for("case_detail", case_id=case_id))


# --- accountant action -----------------------------------------------------
@app.route("/cases/<int:case_id>/payment", methods=["POST"])
@role_required("accountant")
def case_payment(case_id):
    get_case_or_403(case_id)
    form = request.form
    db.execute(
        "INSERT INTO payments (case_id, payer_name, payer_type, amount, purpose, method, reference, logged_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (case_id, form["payer_name"].strip(), form["payer_type"], float(form["amount"]), form["purpose"].strip(),
         form["method"], form["reference"].strip(), g.user["id"], now()),
    )
    flash("Payment logged.", "success")
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/payments")
@role_required("accountant")
def payment_list():
    payments = db.query(
        "SELECT p.*, c.case_number FROM payments p JOIN cases c ON c.id = p.case_id "
        "WHERE c.jurisdiction = ? ORDER BY p.id DESC", (g.user["jurisdiction"],))
    return render_template("payment_list.html", payments=payments)


# ---------------------------------------------------------------------------
# Hearings and files
# ---------------------------------------------------------------------------
@app.route("/hearings")
@role_required("applicant", "clerk", "chief", "assessor")
def hearing_list():
    return render_template("hearing_list.html", hearings=hearings_for_current_user())


# ---------------------------------------------------------------------------
# Video hearing room
# Browsers talk to each other directly (WebRTC). The server only passes small
# "signalling" messages between them and stores the chat.
# ---------------------------------------------------------------------------
ROOM_ROLES = ("applicant", "clerk", "chief", "assessor")
SIGNAL_TYPES = ("join", "leave", "offer", "answer", "candidate", "chat")


def get_hearing_for_room(hearing_id):
    """Return the hearing if the logged-in user may take part in it."""
    hearing = db.query("SELECT * FROM hearings WHERE id = ?", (hearing_id,), one=True) or abort(404)
    get_case_or_403(hearing["case_id"])            # same rules as viewing the case
    if hearing["mode"] != "Online":
        abort(404)
    return hearing


@app.route("/hearings/<int:hearing_id>/room")
@role_required(*ROOM_ROLES)
def hearing_room(hearing_id):
    hearing = get_hearing_for_room(hearing_id)
    case = db.query("SELECT * FROM cases WHERE id = ?", (hearing["case_id"],), one=True)
    files = db.query("SELECT * FROM case_files WHERE case_id = ?", (case["id"],))
    db.execute("DELETE FROM signals WHERE hearing_id = ? AND created_at < datetime('now', '-6 hours')", (hearing_id,))

    room_config = {
        "iceServers": app.config["ICE_SERVERS"],
        "signalUrl": url_for("room_signal", hearing_id=hearing_id),
        "pollUrl": url_for("room_poll", hearing_id=hearing_id),
        "exitUrl": url_for("case_detail", case_id=case["id"]),
        "myName": f"{g.user['first_name']} {g.user['surname']} ({g.user['role'].title()})",
    }
    return render_template("hearing_room.html", hearing=hearing, case=case, files=files, room_config=room_config)


@app.route("/hearings/<int:hearing_id>/signal", methods=["POST"])
@role_required(*ROOM_ROLES)
def room_signal(hearing_id):
    get_hearing_for_room(hearing_id)
    message = request.get_json(silent=True) or {}
    kind = message.get("type")
    payload = json.dumps(message.get("data", {}))
    if kind not in SIGNAL_TYPES or len(payload) > 20000:
        abort(400)
    if kind == "chat":
        payload = json.dumps({"text": str(message.get("data", {}).get("text", ""))[:500]})

    new_id = db.execute(
        "INSERT INTO signals (hearing_id, sender, sender_name, recipient, kind, payload) VALUES (?,?,?,?,?,?)",
        (hearing_id, str(message.get("from", ""))[:20], f"{g.user['first_name']} {g.user['surname']} ({g.user['role'].title()})",
         str(message.get("to", ""))[:20], kind, payload),
    )
    return jsonify(id=new_id)


@app.route("/hearings/<int:hearing_id>/signals")
@role_required(*ROOM_ROLES)
def room_poll(hearing_id):
    """New messages for this browser tab: sent to everyone, or sent only to it."""
    get_hearing_for_room(hearing_id)
    since = request.args.get("since", 0, type=int)
    client = request.args.get("client", "")
    rows = db.query(
        "SELECT * FROM signals WHERE hearing_id = ? AND id > ? AND sender != ? AND (recipient = '' OR recipient = ?) "
        "ORDER BY id", (hearing_id, since, client, client))
    return jsonify([{"id": r["id"], "from": r["sender"], "name": r["sender_name"], "type": r["kind"],
                     "data": json.loads(r["payload"] or "{}")} for r in rows])


@app.route("/files/<int:file_id>")
@login_required
def download_case_file(file_id):
    file = db.query("SELECT * FROM case_files WHERE id = ?", (file_id,), one=True) or abort(404)
    get_case_or_403(file["case_id"])        # same rules as viewing the case
    if g.user["role"] == "accountant":      # accountants see payment info only, not documents
        abort(403)
    return send_from_directory(app.config["UPLOAD_FOLDER"], file["stored_name"],
                               as_attachment=True, download_name=file["filename"])


# ---------------------------------------------------------------------------
# Messenger / sheriff: summons
# ---------------------------------------------------------------------------
@app.route("/summons")
@role_required("messenger")
def summons_list():
    rows = db.query("SELECT s.*, c.case_number FROM summons s JOIN cases c ON c.id = s.case_id "
                    "WHERE s.messenger_id = ? ORDER BY s.id DESC", (g.user["id"],))
    return render_template("summons_list.html", summons_list=rows)


@app.route("/summons/<int:summons_id>/download")
@role_required("messenger", "clerk")
def download_summons(summons_id):
    row = db.query("SELECT * FROM summons WHERE id = ?", (summons_id,), one=True) or abort(404)
    if g.user["role"] == "messenger" and row["messenger_id"] != g.user["id"]:
        abort(403)
    return send_from_directory(app.config["UPLOAD_FOLDER"], row["stored_name"],
                               as_attachment=True, download_name=row["filename"])


@app.route("/summons/<int:summons_id>/served", methods=["POST"])
@role_required("messenger")
def summons_served(summons_id):
    row = db.query("SELECT * FROM summons WHERE id = ? AND messenger_id = ?", (summons_id, g.user["id"]), one=True) or abort(404)
    db.execute("UPDATE summons SET status = 'Served', served_at = ? WHERE id = ?", (now(), summons_id))
    case = db.query("SELECT * FROM cases WHERE id = ?", (row["case_id"],), one=True)
    notify_officials(case["jurisdiction"], ["clerk"], "Summons served",
                     f"{row['recipient_name']} was served for case {case['case_number']}.", case["id"])
    flash("Marked as served. The clerk has been told.", "success")
    return redirect(url_for("summons_list"))


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
@app.route("/notifications", methods=["GET", "POST"])
@login_required
def notification_list():
    if request.method == "POST":                      # "Mark selected as read"
        for note_id in request.form.getlist("selected"):
            db.execute("UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?", (note_id, g.user["id"]))
        return redirect(url_for("notification_list"))
    notes = db.query("SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC", (g.user["id"],))
    return render_template("notifications.html", notes=notes)


@app.route("/notifications/<int:note_id>")
@login_required
def notification_view(note_id):
    note = db.query("SELECT * FROM notifications WHERE id = ? AND user_id = ?", (note_id, g.user["id"]), one=True) or abort(404)
    db.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (note_id,))
    return render_template("notification_view.html", note=note)




# ---------------------------------------------------------------------------
# Account: change password
# ---------------------------------------------------------------------------
@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        # Verify the current password.
        if not check_password_hash(g.user["password_hash"], current_password):
            flash("Your current password is incorrect.", "error")
            return redirect(url_for("change_password"))

        # Validate the new password.
        if len(new_password) < 10:
            flash("Your new password must be at least 10 characters.", "error")
            return redirect(url_for("change_password"))

        if new_password != confirm_password:
            flash("The new passwords do not match.", "error")
            return redirect(url_for("change_password"))

        if new_password == current_password:
            flash("Choose a password different from your current password.", "error")
            return redirect(url_for("change_password"))

        # Save the new password as a secure hash.
        password_hash = generate_password_hash(new_password)

        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, g.user["id"])
        )

        flash("Your password has been changed successfully.", "success")
        return redirect(url_for("change_password"))

    return render_template("change_password.html")

# ---------------------------------------------------------------------------
# System admin: officials and villages
# ---------------------------------------------------------------------------
@app.route("/admin/users", methods=["GET", "POST"])
@role_required("admin")
def admin_users():
    if request.method == "POST":
        form = request.form
        if form["role"] not in OFFICIAL_ROLES:
            abort(400)
        if db.query("SELECT 1 FROM users WHERE username = ?", (form["username"].strip(),), one=True):
            flash("That username is already taken.", "error")
        else:
            db.execute(
                "INSERT INTO users (username, password_hash, first_name, surname, email, phone, role, jurisdiction) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (form["username"].strip(), generate_password_hash(form["password"]), form["first_name"].strip(),
                 form["surname"].strip(), form["email"].strip(), form["phone"].strip(), form["role"], form["jurisdiction"]),
            )
            flash(f"{form['role'].title()} account created.", "success")
        return redirect(url_for("admin_users"))

    officials = db.query("SELECT * FROM users WHERE role IN ({}) ORDER BY jurisdiction, role".format(
        ",".join("?" * len(OFFICIAL_ROLES))), OFFICIAL_ROLES)
    applicants = db.query("SELECT * FROM users WHERE role = 'applicant' ORDER BY id DESC")
    jurisdictions = db.query("SELECT name FROM jurisdictions ORDER BY name")
    return render_template("admin_users.html", officials=officials, applicants=applicants,
                           jurisdictions=jurisdictions, roles=OFFICIAL_ROLES)


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@role_required("admin")
def admin_toggle_user(user_id):
    db.execute("UPDATE users SET active = 1 - active WHERE id = ? AND role != 'admin'", (user_id,))
    return redirect(request.referrer or url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_user(user_id):
    db.execute("DELETE FROM users WHERE id = ? AND role != 'admin'", (user_id,))
    flash("Account deleted.", "success")
    return redirect(request.referrer or url_for("admin_users"))


@app.route("/admin/jurisdictions", methods=["GET", "POST"])
@role_required("admin")
def admin_jurisdictions():
    if request.method == "POST":
        name = request.form["name"].strip()
        if name:
            db.execute("INSERT OR IGNORE INTO jurisdictions (name) VALUES (?)", (name,))
            flash("Village added.", "success")
        return redirect(url_for("admin_jurisdictions"))
    return render_template("admin_jurisdictions.html", jurisdictions=db.query("SELECT * FROM jurisdictions ORDER BY name"))


@app.route("/case-access/<int:request_id>/review", methods=["POST"])
@role_required("clerk")
def case_access_review(request_id):
    decision = request.form.get("decision", "").lower()

    if decision not in ("approve", "reject"):
        abort(400)

    return review_case_access(
        request_id,
        "Approved" if decision == "approve" else "Rejected"
    )


@app.route("/case-request", methods=["POST"])
@role_required("applicant")
def request_case_access():
    case_number = request.form.get("case_number", "").strip().upper()

    if not case_number:
        flash("Please enter a case number.", "error")
        return redirect(request.referrer or url_for("case_list"))

    # Find the case by its case number.
    case = db.query(
        """
        SELECT id, case_number, jurisdiction
        FROM cases
        WHERE UPPER(case_number) = ?
        """,
        (case_number,),
        one=True
    )

    if case is None:
        flash("No case was found with that case number.", "error")
        return redirect(request.referrer or url_for("case_list"))

    # Confirm the logged-in user is listed as a respondent.
    print(g.user["first_name"])
    print(g.user["surname"])
    participant = db.query(
        """
        SELECT id
        FROM case_participants
        WHERE case_id = ?
          AND role = 'Respondent'
          AND LOWER(TRIM(first_name)) = LOWER(TRIM(?))
          AND LOWER(TRIM(surname)) = LOWER(TRIM(?))
        """,
        (case["id"], g.user["first_name"], g.user["surname"]),
        one=True
    )

    if participant is None:
        flash(
            "Your account is not listed as a respondent on this case.",
            "error"
        )
        return redirect(request.referrer or url_for("case_list"))

    # Check whether this user has already requested access.
    existing = db.query(
        """
        SELECT id, status
        FROM case_access_requests
        WHERE case_id = ? AND respondent_id = ?
        """,
        (case["id"], g.user["id"]),
        one=True
    )

    if existing:
        flash(
            f"You already have an access request for this case "
            f"(status: {existing['status']}).",
            "info"
        )
        return redirect(request.referrer or url_for("case_list"))

    # Insert the request. The database default sets status to Pending.
    db.execute(
        """
        INSERT INTO case_access_requests (case_id, respondent_id)
        VALUES (?, ?)
        """,
        (case["id"], g.user["id"])
    )

    # Notify clerks in the case's jurisdiction.
    notify_officials(
        case["jurisdiction"],
        ["clerk"],
        "New case access request",
        f"An applicant has requested access to case {case['case_number']}.",
        case["id"]
    )

    flash("Your case access request has been submitted for clerk review.", "success")
    return redirect(request.referrer or url_for("case_list"))


@app.route("/cases/<int:case_id>/payment-proof", methods=["POST"])
@role_required("applicant")
def submit_payment_proof(case_id):
    # Confirm that the logged-in applicant can access this case.
    case = get_case_or_403(case_id)

    payer_type = request.form.get("payer_type", "").strip()
    method = request.form.get("method", "").strip()
    reference = request.form.get("reference", "").strip()
    notes = request.form.get("notes", "").strip()
    amount_text = request.form.get("amount", "").strip()
    proof = request.files.get("proof_file")

    if payer_type not in ("Applicant", "Respondent"):
        flash("Please select a valid payer type.", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    if method not in ("Mobile money", "Bank", "Online"):
        flash("Please select a valid payment method.", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    if not reference:
        flash("Please enter the transaction reference.", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    try:
        amount = float(amount_text)
        if not (0 < amount < float("inf")):
            raise ValueError
    except (ValueError, TypeError):
        flash("Enter a valid payment amount.", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    if not proof or not proof.filename:
        flash("Please upload your payment receipt.", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    # Check the file extension.
    allowed_extensions = {"pdf", "jpg", "jpeg", "png"}
    original_name = secure_filename(proof.filename)

    if (
        not original_name
        or "." not in original_name
        or original_name.rsplit(".", 1)[1].lower()
        not in allowed_extensions
    ):
        flash("Upload a PDF, JPG, JPEG, or PNG receipt.", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    # Keep payment proofs outside the public static directory.
    upload_dir = os.path.join(current_app.instance_path, "payment_proofs")
    os.makedirs(upload_dir, exist_ok=True)

    extension = original_name.rsplit(".", 1)[1].lower()
    stored_name = f"{uuid.uuid4().hex}.{extension}"
    absolute_path = os.path.join(upload_dir, stored_name)

    # Store the file, then record its path in the database.
    try:
        proof.save(absolute_path)

        # Derive the payer's name from the signed-in account.
        payer_name = (
            f"{g.user['first_name']} {g.user['surname']}"
        ).strip()

        
        db.execute("""
            INSERT INTO submitted_payments (
                case_id,
                payer_name,
                payer_type,
                amount,
                purpose,
                method,
                reference,
                proof_file,
                notes,
                submitted_by,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?)
        """, (
            case_id,
            payer_name,
            payer_type,
            amount,
            notes,
            method,
            reference,
            stored_name,
            notes,
            g.user["id"],
            now()
        ))


    except Exception:
        # Avoid leaving an orphaned file if saving the record fails.
        if os.path.exists(absolute_path):
            os.remove(absolute_path)
        current_app.logger.exception("Could not save payment proof")
        flash("Could not submit your payment proof. Please try again.", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    # Notify accountants in the case's jurisdiction.
    notify_officials(
        case["jurisdiction"],
        ["accountant"],
        "Payment awaiting verification",
        f"Payment proof submitted for case {case['case_number']}.",
        case_id
    )

    flash(
        "Payment proof submitted successfully. It is awaiting accountant verification.",
        "success"
    )
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/submitted-payments/<int:payment_id>/review", methods=["POST"])
@role_required("accountant")
def review_submitted_payment(payment_id):
    decision = request.form.get("decision", "").strip()
    rejection_reason = request.form.get("rejection_reason", "").strip()

    if decision not in ("Approved", "Rejected"):
        flash("Invalid payment review decision.", "error")
        return redirect(request.referrer or url_for("index"))

    conn = db.get_db()

    try:
        # Load the submission and its case.
        submission = conn.execute("""
            SELECT sp.*, c.applicant_id, c.case_number,
                   c.jurisdiction
            FROM submitted_payments AS sp
            JOIN cases AS c ON c.id = sp.case_id
            WHERE sp.id = ?
        """, (payment_id,)).fetchone()

        if not submission:
            flash("Submitted payment not found.", "error")
            return redirect(request.referrer or url_for("index"))

        # Ensure the accountant belongs to the case's jurisdiction.
        user_jurisdiction = g.user["jurisdiction"]
        if (
            user_jurisdiction
            and user_jurisdiction != submission["jurisdiction"]
        ):
            flash("You are not authorized to review this payment.", "error")
            return redirect(url_for("case_detail",
                                    case_id=submission["case_id"]))

        # Do not process the same submission twice.
        if submission["status"] != "Pending":
            flash("This payment submission has already been reviewed.", "error")
            return redirect(url_for("case_detail",
                                    case_id=submission["case_id"]))

        reviewed_at = now()
        reviewer_id = g.user["id"]

        # Approve: record the payment in the existing payments table.
        if decision == "Approved":
            conn.execute("""
                INSERT INTO payments (
                    case_id,
                    payer_name,
                    payer_type,
                    amount,
                    purpose,
                    method,
                    reference,
                    logged_by,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                submission["case_id"],
                submission["payer_name"],
                submission["payer_type"],
                submission["amount"],
                submission["purpose"],
                submission["method"],
                submission["reference"],
                reviewer_id,
                reviewed_at
            ))

            message = (
                f"Your payment of {submission['amount']:.2f} for case "
                f"{submission['case_number']} has been approved and "
                "recorded by the court accountant."
            )
            subject = "Payment approved"

        else:
            # Rejected: no payment is added to the payments table.
            message = (
                f"Your payment proof for case {submission['case_number']} "
                "was not approved. Please review the reason below and "
                "contact the court accountant if you need assistance."
            )

            if rejection_reason:
                message += f" Reason: {rejection_reason}"

            subject = "Payment proof not approved"

        # Update only if it is still pending.
        cursor = conn.execute("""
            UPDATE submitted_payments
            SET status = ?,
                reviewed_by = ?,
                reviewed_at = ?,
                rejection_reason = ?
            WHERE id = ? AND status = 'Pending'
        """, (
            decision,
            reviewer_id,
            reviewed_at,
            rejection_reason if decision == "Rejected" else None,
            payment_id
        ))

        if cursor.rowcount != 1:
            raise RuntimeError("Payment submission was already reviewed.")

        # Notify the applicant through the existing notifications table.
        conn.execute("""
            INSERT INTO notifications (
                user_id, subject, message, case_id, is_read, created_at
            )
            VALUES (?, ?, ?, ?, 0, ?)
        """, (
            submission["applicant_id"],
            subject,
            message,
            submission["case_id"],
            reviewed_at
        ))

        conn.commit()

    except Exception:
        conn.rollback()
        current_app.logger.exception(
            "Failed to review submitted payment %s", payment_id
        )
        flash("Could not process the payment review. Please try again.", "error")
        return redirect(request.referrer or url_for("index"))

    if decision == "Approved":
        flash("Payment approved and added to recorded payments. The applicant has been notified.", "success")
    else:
        flash("Payment disapproved. The applicant has been notified.", "success")

    return redirect(url_for("case_detail", case_id=submission["case_id"]))


@app.route("/submitted-payments/<int:payment_id>/proof")
@login_required
def payment_proof_file(payment_id):
    submission = db.query("""
        SELECT sp.id, sp.case_id, sp.proof_file,
               c.applicant_id, c.jurisdiction
        FROM submitted_payments AS sp
        JOIN cases AS c ON c.id = sp.case_id
        WHERE sp.id = ?
    """, (payment_id,), one=True)

    if not submission:
        abort(404)

    user = g.user
    role = user["role"]

    # Applicants may only access their own case's proof.
    if role == "applicant":
        if submission["applicant_id"] != user["id"]:
            abort(403)

    # Court officials must belong to the case's jurisdiction.
    elif role in ("accountant", "clerk", "chief", "assessor", "messenger"):
        if user["jurisdiction"] != submission["jurisdiction"]:
            abort(403)

    elif role != "admin":
        abort(403)

    filename = submission["proof_file"]
    if not filename:
        abort(404)

    upload_dir = os.path.join(
        current_app.instance_path,
        "payment_proofs"
    )

    file_path = os.path.join(upload_dir, filename)
    if not os.path.isfile(file_path):
        abort(404)

    return send_from_directory(
        upload_dir,
        filename,
        as_attachment=False
    )


@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", code=403, message="You do not have access to this page."), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, message="That page was not found."), 404


if __name__ == "__main__":
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    db.init_db()
    app.run(debug=True)
