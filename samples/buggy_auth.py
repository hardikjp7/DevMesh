"""
Sample buggy file: SQL injection vulnerability.
Use this to test that the pipeline flags CRITICAL issues correctly.
"""

import sqlite3


def get_user(user_id):
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    # BUG: string-formatted SQL query, classic injection vector
    query = "SELECT * FROM users WHERE id = " + user_id
    cursor.execute(query)
    return cursor.fetchone()


def login(username, password):
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    # BUG: same pattern, injection via username/password
    query = f"SELECT * FROM users WHERE username = '{username}' AND password = '{password}'"
    cursor.execute(query)
    return cursor.fetchone()
