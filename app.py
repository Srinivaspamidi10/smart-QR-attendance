from flask import Flask, render_template, request, redirect, url_for, session, Response
import sqlite3, qrcode, uuid, os, math, base64
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import smtplib
from email.message import EmailMessage

app = Flask(__name__)

app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

app.secret_key = "smart_qr_secret_key"

QR_FOLDER = "static/qrcodes"
SELFIE_FOLDER = "static/selfies"

os.makedirs(QR_FOLDER, exist_ok=True)
os.makedirs(SELFIE_FOLDER, exist_ok=True)

ALLOWED_DISTANCE_METERS = 100
def get_db():
    conn = sqlite3.connect("attendance.db", timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def calculate_distance(lat1, lon1, lat2, lon2):
    r = 6371000

    lat1 = math.radians(float(lat1))
    lon1 = math.radians(float(lon1))
    lat2 = math.radians(float(lat2))
    lon2 = math.radians(float(lon2))

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return r * c


def init_db():
    conn = sqlite3.connect("attendance.db")
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS lecturers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT UNIQUE,
        password TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        roll_no TEXT UNIQUE,
        department TEXT,
        email TEXT UNIQUE,
        password TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lecturer_id INTEGER,
        subject TEXT,
        class_name TEXT,
        session_code TEXT UNIQUE,
        created_at TEXT,
        expires_at TEXT,
        latitude TEXT,
        longitude TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_code TEXT,
        student_id INTEGER,
        name TEXT,
        roll_no TEXT,
        department TEXT,
        submitted_at TEXT,
        student_latitude TEXT,
        student_longitude TEXT,
        distance_meters REAL,
        selfie_path TEXT,
        UNIQUE(session_code, roll_no)
    )
    """)

    columns_to_add = [
        ("sessions", "lecturer_id", "INTEGER"),
        ("sessions", "latitude", "TEXT"),
        ("sessions", "longitude", "TEXT"),
        ("attendance", "student_id", "INTEGER"),
        ("attendance", "student_latitude", "TEXT"),
        ("attendance", "student_longitude", "TEXT"),
        ("attendance", "distance_meters", "REAL"),
        ("attendance", "selfie_path", "TEXT")
    ]

    for table, column, col_type in columns_to_add:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/lecturer_register", methods=["GET", "POST"])
def lecturer_register():
    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        password = request.form["password"]

        hashed_password = generate_password_hash(password)

        try:
            conn = sqlite3.connect("attendance.db")
            c = conn.cursor()

            c.execute("""
            INSERT INTO lecturers (name, email, password)
            VALUES (?, ?, ?)
            """, (name, email, hashed_password))

            conn.commit()
            conn.close()

            return redirect(url_for("lecturer_login"))

        except sqlite3.IntegrityError:
            return render_template(
                "message.html",
                title="Registration Failed",
                message="Email already registered!",
                link="/lecturer_register",
                button="Try Again"
            )

    return render_template("lecturer_register.html")


@app.route("/lecturer_login", methods=["GET", "POST"])
def lecturer_login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = sqlite3.connect("attendance.db")
        c = conn.cursor()

        c.execute("""
        SELECT id, name, password
        FROM lecturers
        WHERE email=?
        """, (email,))

        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user[2], password):
            session["lecturer_id"] = user[0]
            session["lecturer_name"] = user[1]
            return redirect(url_for("lecturer_portal"))

        return render_template(
            "message.html",
            title="Login Failed",
            message="Invalid lecturer email or password!",
            link="/lecturer_login",
            button="Try Again"
        )

    return render_template("lecturer_login.html")


@app.route("/lecturer", methods=["GET", "POST"])
def lecturer():
    if "lecturer_id" not in session:
        return redirect(url_for("lecturer_login"))

    if request.method == "POST":
        subject = request.form["subject"]
        class_name = request.form["class_name"]
        expiry_minutes = int(request.form["expiry_minutes"])

        latitude = request.form.get("latitude")
        longitude = request.form.get("longitude")

        if not latitude or not longitude:
            return render_template(
                "message.html",
                title="Location Required",
                message="Please capture class location before generating QR.",
                link="/lecturer",
                button="Try Again"
            )

        unique_code = str(uuid.uuid4())[:8]
        created_at = datetime.now()
        expires_at = created_at + timedelta(minutes=expiry_minutes)

        conn = sqlite3.connect("attendance.db")
        c = conn.cursor()

        c.execute("""
        INSERT INTO sessions
        (lecturer_id, subject, class_name, session_code, created_at, expires_at, latitude, longitude)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session["lecturer_id"],
            subject,
            class_name,
            unique_code,
            created_at.strftime("%Y-%m-%d %H:%M:%S"),
            expires_at.strftime("%Y-%m-%d %H:%M:%S"),
            latitude,
            longitude
        ))

        conn.commit()
        conn.close()

        qr_link = "https://smart-qr-attendance.onrender.com/student/" + unique_code
        qr = qrcode.make(qr_link)
        qr.save(f"{QR_FOLDER}/{unique_code}.png")

        return redirect(url_for("qr_page", code=unique_code))

    return render_template("lecturer.html")


@app.route("/lecturer_sessions")
def lecturer_sessions():
    if "lecturer_id" not in session:
        return redirect(url_for("lecturer_login"))

    search = request.args.get("search", "")

    conn = sqlite3.connect("attendance.db")
    c = conn.cursor()

    c.execute("""
    SELECT subject, class_name, session_code, created_at, expires_at
    FROM sessions
    WHERE lecturer_id=?
    AND (
        subject LIKE ?
        OR class_name LIKE ?
        OR session_code LIKE ?
    )
    ORDER BY id DESC
    """, (
        session["lecturer_id"],
        f"%{search}%",
        f"%{search}%",
        f"%{search}%"
    ))

    sessions_data = c.fetchall()
    conn.close()

    return render_template("lecturer_sessions.html", sessions_data=sessions_data)


@app.route("/qr/<code>")
def qr_page(code):
    if "lecturer_id" not in session:
        return redirect(url_for("lecturer_login"))

    conn = sqlite3.connect("attendance.db")
    c = conn.cursor()

    c.execute("""
    SELECT expires_at
    FROM sessions
    WHERE session_code=?
    """, (code,))

    data = c.fetchone()
    conn.close()

    if data is None:
        return render_template(
            "message.html",
            title="Invalid QR",
            message="This QR session does not exist.",
            link="/lecturer",
            button="Back"
        )

    return render_template("qr.html", code=code, expires_at=data[0])


@app.route("/dashboard/<code>")
def dashboard(code):
    if "lecturer_id" not in session:
        return redirect(url_for("lecturer_login"))

    search = request.args.get("search", "")

    conn = sqlite3.connect("attendance.db")
    c = conn.cursor()

    c.execute("""
    SELECT subject, class_name, created_at, expires_at
    FROM sessions
    WHERE session_code=?
    """, (code,))

    session_info = c.fetchone()

    c.execute("""
    SELECT name, roll_no, department, submitted_at, distance_meters, selfie_path
    FROM attendance
    WHERE session_code=?
    AND (
        name LIKE ?
        OR roll_no LIKE ?
        OR department LIKE ?
    )
    ORDER BY id DESC
    """, (
        code,
        f"%{search}%",
        f"%{search}%",
        f"%{search}%"
    ))

    records = c.fetchall()
    conn.close()

    total_present = len(records)

    return render_template(
        "dashboard.html",
        records=records,
        code=code,
        session_info=session_info,
        total_present=total_present
    )


@app.route("/download/<code>")
def download_attendance(code):
    if "lecturer_id" not in session:
        return redirect(url_for("lecturer_login"))

    conn = sqlite3.connect("attendance.db")
    c = conn.cursor()

    c.execute("""
    SELECT name, roll_no, department, submitted_at, distance_meters, selfie_path
    FROM attendance
    WHERE session_code=?
    """, (code,))

    records = c.fetchall()
    conn.close()

    csv_data = "Name,Roll No,Department,Time,Distance Meters,Selfie Path\n"

    for row in records:
        csv_data += f"{row[0]},{row[1]},{row[2]},{row[3]},{row[4]},{row[5]}\n"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment;filename=attendance_{code}.csv"
        }
    )


@app.route("/student_register", methods=["GET", "POST"])
def student_register():
    if request.method == "POST":
        name = request.form["name"]
        roll_no = request.form["roll_no"]
        department = request.form["department"]
        email = request.form["email"]
        password = request.form["password"]

        hashed_password = generate_password_hash(password)

        try:
            conn = sqlite3.connect("attendance.db")
            c = conn.cursor()

            c.execute("""
            INSERT INTO students
            (name, roll_no, department, email, password)
            VALUES (?, ?, ?, ?, ?)
            """, (
                name,
                roll_no,
                department,
                email,
                hashed_password
            ))

            conn.commit()
            conn.close()

            return redirect(url_for("student_login"))

        except sqlite3.IntegrityError:
            return render_template(
                "message.html",
                title="Registration Failed",
                message="Student already registered!",
                link="/student_register",
                button="Try Again"
            )

    return render_template("student_register.html")


@app.route("/student_login", methods=["GET", "POST"])
def student_login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = sqlite3.connect("attendance.db")
        c = conn.cursor()

        c.execute("""
        SELECT id, name, roll_no, department, password
        FROM students
        WHERE email=?
        """, (email,))

        student = c.fetchone()
        conn.close()

        if student and check_password_hash(student[4], password):
            session["student_id"] = student[0]
            session["student_name"] = student[1]
            session["student_roll"] = student[2]
            session["student_department"] = student[3]

            code = session.get("pending_code")

            if code:
                return redirect(url_for("student", code=code))

            return redirect(url_for("student_portal"))

        return render_template(
            "message.html",
            title="Login Failed",
            message="Invalid student email or password!",
            link="/student_login",
            button="Try Again"
        )

    return render_template("student_login.html")


@app.route("/student/<code>", methods=["GET", "POST"])
def student(code):
    conn = sqlite3.connect("attendance.db")
    c = conn.cursor()

    c.execute("""
    SELECT expires_at, latitude, longitude
    FROM sessions
    WHERE session_code=?
    """, (code,))

    session_data = c.fetchone()

    if session_data is None:
        conn.close()
        return render_template(
            "message.html",
            title="Invalid QR",
            message="This QR code is invalid.",
            link="/",
            button="Go Home"
        )

    expires_at = datetime.strptime(session_data[0], "%Y-%m-%d %H:%M:%S")
    lecturer_latitude = session_data[1]
    lecturer_longitude = session_data[2]

    if datetime.now() > expires_at:
        conn.close()
        return render_template(
            "message.html",
            title="QR Expired",
            message="This QR code has expired.",
            link="/",
            button="Go Home"
        )

    if "student_id" not in session:
        session["pending_code"] = code
        conn.close()
        return redirect(url_for("student_login"))

    if request.method == "POST":
        student_latitude = request.form.get("student_latitude")
        student_longitude = request.form.get("student_longitude")

        if not student_latitude or not student_longitude:
            conn.close()
            return render_template(
                "message.html",
                title="Location Required",
                message="Please capture your location before submitting attendance.",
                link=f"/student/{code}",
                button="Try Again"
            )

        distance = calculate_distance(
            lecturer_latitude,
            lecturer_longitude,
            student_latitude,
            student_longitude
        )

        if distance > ALLOWED_DISTANCE_METERS:
            conn.close()
            return render_template(
                "message.html",
                title="Attendance Rejected",
                message=f"You are too far from class location. Distance: {round(distance, 2)} meters.",
                link="/student_dashboard",
                button="Go Dashboard"
            )

        filename = f"{uuid.uuid4()}.png"
        selfie_path = f"{SELFIE_FOLDER}/{filename}"

        student_id = session["student_id"]
        name = session["student_name"]
        roll_no = session["student_roll"]
        department = session["student_department"]
        submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
    # Prevent duplicate attendance

        c.execute("""
        SELECT * FROM attendance
        WHERE session_code=? AND roll_no=?
        """, (code, roll_no))

        already_marked = c.fetchone()

        if already_marked:

            conn.close()

            return render_template(
    "mark_attendance.html",
    error="⚠️ Attendance already marked. You cannot submit attendance twice.",
    code=code,
    expires_at=expires_at,
    hide_submit=True
)
            
        if distance > 50:

            conn.close()

            return render_template(
                "mark_attendance.html",
                error="🚫 Proxy attendance detected. You are outside the allowed classroom radius.",
                code=code,
                expires_at=expires_at
            )
    
        try:
            c.execute("""
            INSERT INTO attendance
            (session_code, student_id, name, roll_no, department, submitted_at,
            student_latitude, student_longitude, distance_meters, selfie_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                code,
                student_id,
                name,
                roll_no,
                department,
                submitted_at,
                student_latitude,
                student_longitude,
                round(distance, 2),
                selfie_path
            ))

            conn.commit()
            conn.close()

            session.pop("pending_code", None)

            return render_template(
                "message.html",
                title="Success ✅",
                message=f"Attendance submitted successfully! Distance: {round(distance, 2)} meters.",
                link="/student_dashboard",
                button="View My Dashboard"
            )

        except sqlite3.IntegrityError:
            conn.close()
            return render_template(
                "message.html",
                title="Already Submitted",
                message="You already submitted attendance for this session.",
                link="/student_dashboard",
                button="View My Dashboard"
            )

    conn.close()
    return render_template("student.html", expires_at=session_data[0])


@app.route("/student_dashboard")
def student_dashboard():
    if "student_id" not in session:
        return redirect(url_for("student_login"))

    conn = sqlite3.connect("attendance.db")
    c = conn.cursor()

    c.execute("""
    SELECT sessions.subject,
        sessions.class_name,
        attendance.submitted_at
    FROM attendance
    JOIN sessions
    ON attendance.session_code = sessions.session_code
    WHERE attendance.student_id=?
    ORDER BY attendance.id DESC
    """, (session["student_id"],))

    records = c.fetchall()

    c.execute("""
    SELECT subject, COUNT(*)
    FROM sessions
    GROUP BY subject
    """)

    subject_totals = c.fetchall()

    c.execute("""
    SELECT sessions.subject, COUNT(*)
    FROM attendance
    JOIN sessions
    ON attendance.session_code = sessions.session_code
    WHERE attendance.student_id=?
    GROUP BY sessions.subject
    """, (session["student_id"],))

    student_subjects = c.fetchall()

    subject_percentages = []

    for total in subject_totals:
        subject_name = total[0]
        total_classes = total[1]
        attended = 0

        for s in student_subjects:
            if s[0] == subject_name:
                attended = s[1]

        percentage = round((attended / total_classes) * 100, 2)

        subject_percentages.append(
            (
                subject_name,
                attended,
                total_classes,
                percentage
            )
        )

    c.execute("SELECT COUNT(*) FROM sessions")
    total_sessions = c.fetchone()[0]

    conn.close()

    total_present = len(records)

    if total_sessions > 0:
        percentage = round((total_present / total_sessions) * 100, 2)
    else:
        percentage = 0

    return render_template(
        "student_dashboard.html",
        records=records,
        total_present=total_present,
        total_sessions=total_sessions,
        percentage=percentage,
        subject_percentages=subject_percentages
    )


@app.route("/delete_session/<code>")
def delete_session(code):
    if "lecturer_id" not in session:
        return redirect(url_for("lecturer_login"))

    conn = sqlite3.connect("attendance.db")
    c = conn.cursor()

    c.execute("DELETE FROM attendance WHERE session_code=?", (code,))

    c.execute("""
    DELETE FROM sessions
    WHERE session_code=?
    AND lecturer_id=?
    """, (code, session["lecturer_id"]))

    conn.commit()
    conn.close()

    qr_path = f"{QR_FOLDER}/{code}.png"

    if os.path.exists(qr_path):
        os.remove(qr_path)

    return redirect(url_for("lecturer_sessions"))


@app.route("/extend_session/<code>/<int:minutes>")
def extend_session(code, minutes):
    if "lecturer_id" not in session:
        return redirect(url_for("lecturer_login"))

    conn = sqlite3.connect("attendance.db")
    c = conn.cursor()

    new_expiry = datetime.now() + timedelta(minutes=minutes)

    c.execute("""
    UPDATE sessions
    SET expires_at=?
    WHERE session_code=?
    AND lecturer_id=?
    """, (
        new_expiry.strftime("%Y-%m-%d %H:%M:%S"),
        code,
        session["lecturer_id"]
    ))

    conn.commit()
    conn.close()

    return redirect(url_for("qr_page", code=code))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.route("/scan_qr")
def scan_qr():
    if "student_id" not in session:
        return redirect(url_for("student_login"))

    return render_template("scan_qr.html")

@app.route("/student_portal")
def student_portal():
    if "student_id" not in session:
        return redirect(url_for("student_login"))

    return render_template("student_portal.html")

@app.route("/lecturer_portal")
def lecturer_portal():

    if "lecturer_id" not in session:
        return redirect(url_for("lecturer_login"))

    return render_template("lecturer_portal.html")

@app.route("/download_pdf/<code>")
def download_pdf(code):
    if "lecturer_id" not in session:
        return redirect(url_for("lecturer_login"))

    conn = get_db()
    c = conn.cursor()

    c.execute("""
    SELECT subject, class_name, created_at, expires_at
    FROM sessions
    WHERE session_code=?
    """, (code,))
    session_info = c.fetchone()

    c.execute("""
    SELECT name, roll_no, department, submitted_at, distance_meters
    FROM attendance
    WHERE session_code=?
    """, (code,))
    records = c.fetchall()

    conn.close()

    filename = f"attendance_{code}.pdf"
    filepath = f"static/{filename}"

    pdf = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4

    y = height - inch

    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(50, y, "Smart QR Attendance Report")

    y -= 40
    pdf.setFont("Helvetica", 11)

    if session_info:
        pdf.drawString(50, y, f"Subject: {session_info[0]}")
        y -= 20
        pdf.drawString(50, y, f"Class: {session_info[1]}")
        y -= 20
        pdf.drawString(50, y, f"Created: {session_info[2]}")
        y -= 20
        pdf.drawString(50, y, f"Expires: {session_info[3]}")

    y -= 40

    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(50, y, "Name")
    pdf.drawString(150, y, "Roll No")
    pdf.drawString(240, y, "Department")
    pdf.drawString(340, y, "Time")
    pdf.drawString(470, y, "Distance")

    y -= 20
    pdf.setFont("Helvetica", 9)

    for row in records:
        if y < 80:
            pdf.showPage()
            y = height - inch
            pdf.setFont("Helvetica", 9)

        pdf.drawString(50, y, str(row[0])[:15])
        pdf.drawString(150, y, str(row[1])[:12])
        pdf.drawString(240, y, str(row[2])[:12])
        pdf.drawString(340, y, str(row[3])[:18])
        pdf.drawString(470, y, f"{row[4]} m")

        y -= 20

    pdf.save()

    return redirect("/" + filepath)

@app.route("/send_report/<code>")
def send_report(code):

    if "lecturer_id" not in session:
        return redirect(url_for("lecturer_login"))

    conn = get_db()
    c = conn.cursor()

    c.execute("""
    SELECT email
    FROM lecturers
    WHERE id=?
    """, (session["lecturer_id"],))

    lecturer = c.fetchone()

    conn.close()

    if not lecturer:
        return "Lecturer email not found"

    lecturer_email = lecturer[0]

    pdf_path = f"static/attendance_{code}.pdf"

    msg = EmailMessage()

    msg["Subject"] = "Smart QR Attendance Report"

    msg["From"] = "YOUR_GMAIL@gmail.com"

    msg["To"] = lecturer_email

    msg.set_content(
        "Attached is your attendance report PDF."
    )

    with open(pdf_path, "rb") as f:

        file_data = f.read()

        msg.add_attachment(
            file_data,
            maintype="application",
            subtype="pdf",
            filename=f"attendance_{code}.pdf"
        )

    server = smtplib.SMTP(
        "smtp.gmail.com",
        587
    )

    server.starttls()

    server.login(
        "YOUR_GMAIL@gmail.com",
        "YOUR_APP_PASSWORD"
    )

    server.send_message(msg)

    server.quit()

    return render_template(
        "message.html",
        title="Email Sent",
        message="Attendance report emailed successfully.",
        link=f"/dashboard/{code}",
        button="Back to Dashboard"
    )
    
    # your old routes here

@app.route("/admin_login", methods=["GET", "POST"])
def admin_login():

    if request.method == "POST":

        username = request.form["username"]
        password = request.form["password"]

        if username == "admin" and password == "admin123":

            session["admin"] = "admin"

            return redirect("/admin_panel")

        else:
            return "Invalid Admin Login"

    return render_template("admin_login.html")


@app.route("/admin_panel")
def admin_panel():

    if "admin" not in session:
        return redirect("/admin_login")

    conn = sqlite3.connect("attendance.db")
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM lecturers")
    lecturers_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM students")
    students_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM sessions")
    sessions_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM attendance")
    attendance_count = cur.fetchone()[0]

    conn.close()

    return render_template(
        "admin_panel.html",
        lecturers_count=lecturers_count,
        students_count=students_count,
        sessions_count=sessions_count,
        attendance_count=attendance_count
    )

print(app.url_map)

@app.route("/live_monitor/<session_code>")
def live_monitor(session_code):

    conn = sqlite3.connect("attendance.db")
    cur = conn.cursor()

    cur.execute("""
        SELECT subject, class_name
        FROM sessions
        WHERE session_code=?
    """, (session_code,))

    session_data = cur.fetchone()

    if not session_data:
        conn.close()
        return "Session not found"

    subject = session_data[0]
    class_name = session_data[1]

    cur.execute("""
        SELECT name, roll_no, department, submitted_at, latitude, longitude
        FROM attendance
        WHERE session_code=?
        ORDER BY submitted_at DESC
    """, (session_code,))

    records = cur.fetchall()

    total = len(records)

    conn.close()

    return render_template(
        "live_monitor.html",
        records=records,
        total=total,
        subject=subject,
        class_name=class_name
    )
    
@app.route("/send_session_email/<session_code>")
def send_session_email(session_code):

    conn = sqlite3.connect("attendance.db")
    cur = conn.cursor()

    cur.execute("""
        SELECT subject, class_name
        FROM sessions
        WHERE session_code=?
    """, (session_code,))

    session_data = cur.fetchone()

    if not session_data:
        conn.close()
        return "Session not found"

    subject = session_data[0]
    class_name = session_data[1]

    cur.execute("SELECT email FROM students")
    students = cur.fetchall()

    conn.close()

    attendance_link = "https://smart-qr-attendance.onrender.com/student/" + session_code
    qr_link = "https://smart-qr-attendance.onrender.com/student/" + session_code

    sender_email = "YOUR_EMAIL@gmail.com"
    app_password = "YOUR_APP_PASSWORD"

    for student in students:

        msg = EmailMessage()

        msg["Subject"] = "Attendance Link - " + subject

        msg["From"] = sender_email

        msg["To"] = student[0]

        msg.set_content(f"""
    Hello Student,

    Attendance session is active.

    Subject: {subject}
    Class: {class_name}

    Mark your attendance using this link:
    {attendance_link}

    Thank you.
    """)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:

            smtp.login(sender_email, app_password)

            smtp.send_message(msg)

    return "Attendance email sent to all students"

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)