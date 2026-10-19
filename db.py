"""Database helpers: connection, simple query functions, schema and seed data."""
import os
import sqlite3

from flask import g
from werkzeug.security import generate_password_hash

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chiefs_court.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jurisdictions (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    first_name    TEXT NOT NULL,
    surname       TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    role          TEXT NOT NULL,      -- admin, chief, clerk, assessor, accountant, messenger, applicant
    jurisdiction  TEXT,               -- only for officials
    active        INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cases (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    case_number      TEXT UNIQUE NOT NULL,
    applicant_id     INTEGER NOT NULL,
    applicant_name   TEXT NOT NULL,
    applicant_surname TEXT NOT NULL,
    jurisdiction     TEXT NOT NULL,
    participants     TEXT,
    cause_of_action  TEXT NOT NULL,
    description      TEXT,
    status           TEXT DEFAULT 'Pending',
    denial_reason    TEXT,
    decision         TEXT,
    decided_by       INTEGER,
    decided_at       TEXT,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS case_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER NOT NULL,
    filename    TEXT NOT NULL,        -- original name shown to users
    stored_name TEXT NOT NULL,        -- unique name on disk
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hearings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      INTEGER NOT NULL,
    hearing_date TEXT NOT NULL,
    hearing_time TEXT NOT NULL,
    mode         TEXT NOT NULL,       -- Physical or Online
    location     TEXT,
    meeting_link TEXT,                -- no longer used (kept so older databases still work)
    created_by   INTEGER,
    room_code    TEXT                 -- set for Online hearings
);

-- Messages passed between people in a video hearing room (WebRTC signalling and chat)
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hearing_id  INTEGER NOT NULL,
    sender      TEXT NOT NULL,        -- random id of the browser tab that sent it
    sender_name TEXT NOT NULL,
    recipient   TEXT DEFAULT '',      -- empty means "everyone in the room"
    kind        TEXT NOT NULL,        -- join, leave, offer, answer, candidate, chat
    payload     TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS summons (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id        INTEGER NOT NULL,
    messenger_id   INTEGER NOT NULL,
    recipient_name TEXT NOT NULL,
    notes          TEXT,
    filename       TEXT NOT NULL,
    stored_name    TEXT NOT NULL,
    status         TEXT DEFAULT 'Pending',   -- Pending or Served
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    served_at      TEXT
);

CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    INTEGER NOT NULL,
    payer_name TEXT NOT NULL,
    payer_type TEXT NOT NULL,         -- Applicant or Respondent
    amount     REAL NOT NULL,
    purpose    TEXT,
    method     TEXT,
    reference  TEXT,
    logged_by  INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    subject    TEXT NOT NULL,
    message    TEXT,
    case_id    INTEGER,
    is_read    INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

 CREATE TABLE IF NOT EXISTS case_participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id INTEGER NOT NULL,
        role TEXT NOT NULL
            CHECK (role IN ('Respondent', 'Witness')),
        first_name TEXT NOT NULL,
        surname TEXT NOT NULL,
        FOREIGN KEY (case_id) REFERENCES cases(id)
            ON DELETE CASCADE
    )
"""


def get_db():
    """One connection per request."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(error=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query(sql, args=(), one=False):
    """Run a SELECT. Returns a list of rows, or a single row when one=True."""
    rows = get_db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    """Run an INSERT/UPDATE/DELETE and return the new row id."""
    conn = get_db()
    cursor = conn.execute(sql, args)
    conn.commit()
    return cursor.lastrowid


def init_db():
    """Create tables and a first admin account (admin / admin123) plus sample villages."""
    conn = sqlite3.connect(DATABASE)
    conn.executescript(SCHEMA)

    # Older databases: add the room_code column if it is missing
    columns = [row[1] for row in conn.execute("PRAGMA table_info(hearings)")]
    if "room_code" not in columns:
        conn.execute("ALTER TABLE hearings ADD COLUMN room_code TEXT")

    has_admin = conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone()
    if not has_admin:
        conn.execute(
            "INSERT INTO users (username, password_hash, first_name, surname, role) "
            "VALUES (?, ?, ?, ?, ?)",
            ("admin", generate_password_hash("admin123"), "System", "Admin", "admin"),
        )
        for village in ("Chikwaka", "Mhondoro", "Chiweshe"):
            conn.execute("INSERT OR IGNORE INTO jurisdictions (name) VALUES (?)", (village,))
    conn.commit()
    conn.close()
