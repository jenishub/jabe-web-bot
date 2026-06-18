import io
import json
import math
import os
import smtplib
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, Response, redirect, render_template, request,
                   session, url_for)
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "jabe-secret-change-in-prod")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")
HIDDEN_EMAILS = ["vip@jabe.kz", "production@jabe.kz"]
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

COUNTER_FILE = os.path.join(DATA_DIR, "counter.json")
QUOTES_FILE = os.path.join(DATA_DIR, "quotes.json")

# Users: "user1:pass1,user2:pass2" in USERS env var
_raw_users = os.getenv("USERS", "")
USERS = {}
for entry in _raw_users.split(","):
    entry = entry.strip()
    if ":" in entry:
        u, p = entry.split(":", 1)
        USERS[u.strip()] = p.strip()

with open("rates.json") as f:
    RATES = json.load(f)

HOTELS = RATES["hotels"]
LOCATIONS = ["Almaty", "Shymbulak", "Kolsay"]
TICKETS = RATES["tickets"]
MEALS = RATES["meals"]
VEHICLES = RATES["vehicles"]
TRANSPORT = RATES["transport"]
SUV = RATES.get("suv", {"cost": 20000, "capacity": 5, "tour_keyword": "kaindy"})
DRIVER_STAY = RATES.get("driver_stay", {"solo": 15000, "both": 30000})
SHYMBULAK_CAB = RATES.get("shymbulak_cab", {"cost": 4000, "capacity": 4})
TOUR_LIST = list(TRANSPORT.keys())
ARRIVAL_TOURS = [t for t in TOUR_LIST if t.lower().startswith("arrival")]
DEPARTURE_TOURS = [t for t in TOUR_LIST if t.lower().startswith("departure")]
MIDDLE_TOURS = [t for t in TOUR_LIST if t not in ARRIVAL_TOURS and t not in DEPARTURE_TOURS]
ROOM_LABELS = ["Single", "Double", "Triple"]


# ---------- helpers ----------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def next_code():
    if os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE) as f:
            data = json.load(f)
    else:
        data = {"counter": 0}
    data["counter"] += 1
    with open(COUNTER_FILE, "w") as f:
        json.dump(data, f)
    return f"JBC{data['counter']:03d}"


def save_quote(record):
    quotes = []
    if os.path.exists(QUOTES_FILE):
        try:
            with open(QUOTES_FILE) as f:
                quotes = json.load(f)
        except Exception:
            quotes = []
    quotes.append(record)
    with open(QUOTES_FILE, "w") as f:
        json.dump(quotes, f, indent=2)


def classify_age(age):
    if age <= 4:
        return "free"
    if age <= 10:
        return "half_both"
    if age <= 12:
        return "half_ticket"
    return "adult"


def summarize_children(ages):
    counts = {"free": 0, "half_both": 0, "half_ticket": 0, "adult": 0}
    for a in ages:
        counts[classify_age(a)] += 1
    return counts


def tickets_for_tour(tour_name):
    return [(k, v) for k, v in TICKETS.items() if k.lower() in tour_name.lower()]


def fmt_kzt(x):
    return f"{int(round(x)):,} KZT"


def fmt_usd(x):
    return f"{x:,.0f} USD"


def tours_for_day(day_num, total_days):
    if day_num == 1:
        return ARRIVAL_TOURS
    if day_num == total_days:
        return DEPARTURE_TOURS
    return MIDDLE_TOURS


# ---------- calculation (same logic as bot) ----------
def calculate(data):
    adult_count = data["adult_count"]
    cc = data["child_counts"]
    days = data["days"]
    rate = data["exchange_rate"]
    vcol = VEHICLES[data["vehicle"]]["col"]
    vehicle_has_guide = "guide" in VEHICLES[data["vehicle"]]["name"].lower()
    mode = data.get("hotel_mode", "include")
    nights_split = data.get("nights_split", {})
    sel = data.get("sel", {})
    rooms_sel = data.get("rooms", [])

    hotel_results = []
    if mode == "include":
        for loc in LOCATIONS:
            loc_nights = nights_split.get(loc, 0)
            if loc == "Almaty" and data.get("early_checkin"):
                loc_nights += 1
            if loc_nights <= 0:
                continue
            loc_hotels = []
            for hi in sel.get(loc, []):
                h = HOTELS[loc][hi]
                rooms = {}
                for idx, (label, key, occ) in enumerate(
                        [("Single", "single", 1), ("Double", "double", 2), ("Triple", "triple", 3)]):
                    if idx not in rooms_sel:
                        continue
                    room_rate = h.get(key)
                    if not room_rate:
                        continue
                    per_pax = room_rate / occ * loc_nights
                    if h["payment"] == "Cash":
                        per_pax *= 1.04
                    rooms[label] = math.ceil(per_pax / rate)
                if rooms:
                    loc_hotels.append({"name": h["name"], "rooms": rooms, "payment": h["payment"]})
            if loc_hotels:
                hotel_results.append({"location": loc, "nights": loc_nights, "hotels": loc_hotels})

    transport_total = tickets_per_pax = extra_minivan_total = suv_total = suv_count = 0
    ticket_lines = []
    seat_count = data["seat_count"]
    seats_needed = data.get("seats_needed", seat_count + 1)

    for tour in data["tours"]:
        transport_total += TRANSPORT[tour][vcol]
        for tname, tprice in tickets_for_tour(tour):
            tickets_per_pax += tprice
            ticket_lines.append(f"{tname}: {fmt_kzt(tprice)} per pax")
        if seat_count >= 14 and "transfer" in tour.lower():
            extra_minivan_total += TRANSPORT[tour][2]
        if SUV["tour_keyword"].lower() in tour.lower():
            n = math.ceil(seats_needed / SUV["capacity"])
            suv_count += n
            suv_total += n * SUV["cost"]

    kolsay_nights = nights_split.get("Kolsay", 0) if mode == "include" else 0
    stay_rate = DRIVER_STAY["both"] if vehicle_has_guide else DRIVER_STAY["solo"]
    driver_stay_total = kolsay_nights * stay_rate

    shymbulak_cab_total = shymbulak_cab_count = 0
    if mode == "include":
        shymbulak_sel = any(
            HOTELS["Shymbulak"][hi]["name"] == SHYMBULAK_CAB.get("hotel_keyword", "Shymbulak Ski Resort Hotel")
            for hi in sel.get("Shymbulak", [])
        )
        if shymbulak_sel:
            shymbulak_cab_count = math.ceil(seat_count / SHYMBULAK_CAB["capacity"])
            shymbulak_cab_total = shymbulak_cab_count * SHYMBULAK_CAB["cost"]

    lunches, dinners, galas = data["lunches"], data["dinners"], data["galas"]
    meals_per_pax = lunches * MEALS["lunch"] + dinners * MEALS["dinner"] + galas * MEALS["gala"]

    alcohol_per_pax = 0
    if galas > 0 and data.get("alcohol") == "local":
        alcohol_per_pax = MEALS["alcohol_local"] * galas
    elif galas > 0 and data.get("alcohol") == "premium":
        alcohol_per_pax = MEALS["alcohol_premium"] * galas

    shared_flat = 0
    if galas > 0 and data.get("dj") == "gala":
        shared_flat += MEALS["dj_gala"]
    elif galas > 0 and data.get("dj") == "conference":
        shared_flat += MEALS["dj_gala_conference"]
    if galas > 0 and data.get("dancers"):
        shared_flat += MEALS["dancers"]

    water_per_pax = MEALS["water_per_day"] * days
    markup = data["markup"]
    shared_total = transport_total + extra_minivan_total + shared_flat + suv_total + driver_stay_total + shymbulak_cab_total
    shared_per_adult = shared_total / adult_count if adult_count else 0

    def to_usd(kzt):
        return kzt * 1.04 / rate

    adult_land_kzt = shared_per_adult + tickets_per_pax + meals_per_pax + alcohol_per_pax + water_per_pax
    adult_final = math.ceil(to_usd(adult_land_kzt) + markup)
    c510_kzt = 0.5 * tickets_per_pax + 0.5 * meals_per_pax + water_per_pax
    c510_final = math.ceil(to_usd(c510_kzt) + markup)
    c1112_kzt = 0.5 * tickets_per_pax + meals_per_pax + water_per_pax
    c1112_final = math.ceil(to_usd(c1112_kzt) + markup)

    return {
        "hotel_results": hotel_results,
        "transport_total": transport_total,
        "extra_minivan_total": extra_minivan_total,
        "suv_total": suv_total, "suv_count": suv_count,
        "driver_stay_total": driver_stay_total, "kolsay_nights": kolsay_nights,
        "shymbulak_cab_total": shymbulak_cab_total, "shymbulak_cab_count": shymbulak_cab_count,
        "shared_flat": shared_flat, "shared_per_adult": shared_per_adult,
        "tickets_per_pax": tickets_per_pax, "ticket_lines": ticket_lines,
        "meals_per_pax": meals_per_pax, "alcohol_per_pax": alcohol_per_pax,
        "water_per_pax": water_per_pax, "markup": markup,
        "adult_count": adult_count, "child_counts": cc,
        "adult_land_kzt": adult_land_kzt,
        "adult_usd_before_markup": to_usd(adult_land_kzt),
        "adult_final": adult_final, "c510_final": c510_final, "c1112_final": c1112_final,
    }


def build_pdf(code, data, calc):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=styles["Title"], fontSize=18, spaceAfter=6)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13, spaceBefore=12, spaceAfter=4)
    body = ParagraphStyle("B", parent=styles["Normal"], fontSize=11, leading=15)
    cc = calc["child_counts"]
    s = []

    s.append(Paragraph("JABE CONCIERGE", title))
    s.append(Paragraph(f"Proposal {code}", styles["Heading3"]))
    s.append(Paragraph(f"Travel dates: {data['dates_text']} ({data['nights']} nights / {data['days']} days)", body))
    paying = cc["half_both"] + cc["half_ticket"]
    s.append(Paragraph(
        f"Group size: {calc['adult_count']} adult(s)"
        + (f", {paying} child(ren)" if paying else "")
        + (f", {cc['free']} infant(s) free" if cc["free"] else ""), body))
    s.append(Spacer(1, 10))

    mode = data.get("hotel_mode", "include")
    if mode == "include":
        s.append(Paragraph("A) Hotel Part Cost (nett rate)", h2))
        for loc in calc["hotel_results"]:
            s.append(Paragraph(f"<b>{loc['location']} — {loc['nights']} night(s)</b>", body))
            for h in loc["hotels"]:
                s.append(Paragraph(h["name"], body))
                for label, usd in h["rooms"].items():
                    s.append(Paragraph(f"&nbsp;&nbsp;&nbsp;{label}: {fmt_usd(usd)} per 1 pax", body))
            s.append(Spacer(1, 6))
        early = " (with early check-in)" if data.get("early_checkin") else " (without early check-in or late check-out)"
        s.append(Paragraph("<b>Inclusions:</b>", body))
        s.append(Paragraph(f"• Accommodation as listed{early}", body))
        s.append(Paragraph("• Daily breakfast", body))
    elif mode == "high_season":
        s.append(Paragraph("A) Hotel Part Cost", h2))
        s.append(Paragraph(
            "September is peak high season. Please confirm actual rates and availability "
            "with <b>sales@jabe.kz</b>.", body))
    else:
        s.append(Paragraph("A) Hotel Part Cost", h2))
        s.append(Paragraph("Hotel not included — accommodation arranged directly by the guest.", body))

    s.append(Paragraph("B) Land Part Cost", h2))
    s.append(Paragraph(f"<b>Adult: {fmt_usd(calc['adult_final'])} per 1 pax</b>", body))
    if cc["half_both"]:
        s.append(Paragraph(f"<b>Child (5-10 y.o.): {fmt_usd(calc['c510_final'])} per 1 pax</b>", body))
    if cc["half_ticket"]:
        s.append(Paragraph(f"<b>Child (11-12 y.o.): {fmt_usd(calc['c1112_final'])} per 1 pax</b>", body))
    if cc["free"]:
        s.append(Paragraph("<b>Child (1-4 y.o.): complimentary</b>", body))
    s.append(Spacer(1, 4))
    s.append(Paragraph("<b>Inclusions:</b>", body))
    s.append(Paragraph("• All transfers PVT", body))
    veh = VEHICLES[data["vehicle"]]
    s.append(Paragraph(
        f"• {'English speaking driver or guide' if veh['english'] else 'Driver (non-English speaking)'} ({veh['name']})", body))
    for i, tour in enumerate(data["tours"], 1):
        s.append(Paragraph(f"• Day {i}: {tour}", body))
    if data["lunches"]:
        s.append(Paragraph(f"• {data['lunches']} x Lunch", body))
    if data["dinners"]:
        s.append(Paragraph(f"• {data['dinners']} x Dinner", body))
    if data["galas"]:
        s.append(Paragraph(f"• {data['galas']} x Gala Dinner", body))
        if data.get("alcohol") == "local":
            s.append(Paragraph("• Alcohol package (local)", body))
        elif data.get("alcohol") == "premium":
            s.append(Paragraph("• Alcohol package (premium)", body))
        if data.get("dj") in ("gala", "conference"):
            s.append(Paragraph("• DJ", body))
        if data.get("dancers"):
            s.append(Paragraph("• Dance show (2 dancers)", body))
    if calc["ticket_lines"]:
        s.append(Paragraph("• Entrance tickets included for selected tours", body))
    if calc.get("suv_count"):
        s.append(Paragraph("• 4x4 SUV transfer at Kaindy Lake", body))
    if calc.get("kolsay_nights"):
        s.append(Paragraph("• Driver/guide overnight stay at Kolsay", body))
    if calc.get("shymbulak_cab_count"):
        s.append(Paragraph("• Return cab transfer at Shymbulak", body))
    s.append(Paragraph("• Daily water", body))
    if paying or cc["free"]:
        s.append(Spacer(1, 6))
        s.append(Paragraph(
            "<i>Child pricing: 1-4 y.o. free; 5-10 y.o. 50% off tickets &amp; meals; "
            "11-12 y.o. 50% off tickets; 13 y.o. and above charged as adult.</i>", body))

    issue_date = datetime.now().strftime("%d %B %Y")
    s.append(Spacer(1, 12))
    roe = ParagraphStyle("ROE", parent=body, fontSize=9, textColor="#555555")
    s.append(Paragraph(
        f"Based on ROE {data['exchange_rate']} KZT/USD as of {issue_date}. "
        "Subject to change to the actual ROE before confirmation.", roe))

    doc.build(s)
    buf.seek(0)
    return buf


def send_hidden_email(code, data, calc, pdf_buf):
    cc = calc["child_counts"]
    lines = [
        f"Calculation {code}",
        f"Dates: {data['dates_text']} ({data['nights']} nights / {data['days']} days)",
        f"Hotel mode: {data.get('hotel_mode','include')}",
        f"Adults: {calc['adult_count']} | C5-10: {cc['half_both']} | C11-12: {cc['half_ticket']} | Infants: {cc['free']}",
        f"Exchange rate: {data['exchange_rate']} KZT/USD",
        f"Vehicle: {VEHICLES[data['vehicle']]['name']}",
        "",
        "=== COST BREAKDOWN (KZT, nett — 4% applied at totals) ===",
        f"Transport total: {fmt_kzt(calc['transport_total'])}",
    ]
    if calc["extra_minivan_total"]:
        lines.append(f"Extra minivan: {fmt_kzt(calc['extra_minivan_total'])}")
    if calc.get("suv_total"):
        lines.append(f"SUV at Kaindy ({calc['suv_count']} x {fmt_kzt(SUV['cost'])}): {fmt_kzt(calc['suv_total'])}")
    if calc.get("driver_stay_total"):
        lines.append(f"Driver/guide Kolsay stay ({calc['kolsay_nights']}n): {fmt_kzt(calc['driver_stay_total'])}")
    if calc.get("shymbulak_cab_total"):
        lines.append(f"Shymbulak cab ({calc['shymbulak_cab_count']} x {fmt_kzt(SHYMBULAK_CAB['cost'])}): {fmt_kzt(calc['shymbulak_cab_total'])}")
    if calc["shared_flat"]:
        lines.append(f"DJ/Dancers: {fmt_kzt(calc['shared_flat'])}")
    lines += [
        f"Per adult (shared): {fmt_kzt(calc['shared_per_adult'])}",
        f"Tickets per pax: {fmt_kzt(calc['tickets_per_pax'])}",
        f"Meals per pax: {fmt_kzt(calc['meals_per_pax'])}",
        f"Water per pax: {fmt_kzt(calc['water_per_pax'])}",
        "",
        "=== LAND TOTALS ===",
        f"Nett subtotal (KZT): {fmt_kzt(calc['adult_land_kzt'])}",
        f"+ 4% tax (KZT): {fmt_kzt(calc['adult_land_kzt'] * 1.04)}",
        f"Before markup (USD): {calc['adult_usd_before_markup']:.2f}",
        f"Markup: {calc['markup']:.2f} USD",
        f"Adult FINAL: {calc['adult_final']} USD",
    ]
    if cc["half_both"]:
        lines.append(f"Child 5-10 FINAL: {calc['c510_final']} USD")
    if cc["half_ticket"]:
        lines.append(f"Child 11-12 FINAL: {calc['c1112_final']} USD")

    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(HIDDEN_EMAILS)
    msg["Subject"] = code
    msg.attach(MIMEText("\n".join(lines), "plain"))
    pdf_buf.seek(0)
    part = MIMEApplication(pdf_buf.read(), Name=f"{code}.pdf")
    part["Content-Disposition"] = f'attachment; filename="{code}.pdf"'
    msg.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, HIDDEN_EMAILS, msg.as_string())


# ---------- routes ----------
@app.route("/", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "").strip()
        if USERS.get(u) == p:
            session["user"] = u
            session.permanent = False
            return redirect(url_for("step_rate"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/calc/rate", methods=["GET", "POST"])
@login_required
def step_rate():
    error = None
    if request.method == "POST":
        try:
            rate = float(request.form["rate"].replace(",", "."))
            session["calc"] = {"exchange_rate": rate}
            return redirect(url_for("step_pax"))
        except (ValueError, KeyError):
            error = "Please enter a valid number, e.g. 530"
    return render_template("step_rate.html", error=error)


@app.route("/calc/pax", methods=["GET", "POST"])
@login_required
def step_pax():
    error = None
    if request.method == "POST":
        try:
            adults = int(request.form["adults"])
            if adults < 1:
                raise ValueError
            child_ages_raw = request.form.get("child_ages", "").strip()
            ages = []
            if child_ages_raw:
                for a in child_ages_raw.replace(",", " ").split():
                    age = int(a)
                    if age < 0 or age > 17:
                        raise ValueError(f"Invalid age: {age}")
                    ages.append(age)
            counts = summarize_children(ages)
            adult_count = adults + counts["adult"]
            children_5plus = counts["half_both"] + counts["half_ticket"] + counts["adult"]
            seat_count = adults + children_5plus
            calc = session.get("calc", {})
            calc.update({
                "adults_entered": adults,
                "child_ages": ages,
                "child_counts": counts,
                "adult_count": adult_count,
                "seat_count": seat_count,
                "seats_needed": seat_count + 1,
            })
            session["calc"] = calc
            return redirect(url_for("step_dates"))
        except ValueError as e:
            error = str(e) if "age" in str(e).lower() else "Please enter valid numbers."
    return render_template("step_pax.html", error=error)


@app.route("/calc/dates", methods=["GET", "POST"])
@login_required
def step_dates():
    error = None
    if request.method == "POST":
        try:
            nights = int(request.form["nights"])
            if nights < 1:
                raise ValueError
            dates_text = request.form.get("dates_text", "").strip()
            is_sep = "sep" in dates_text.lower()
            calc = session.get("calc", {})
            calc.update({"nights": nights, "days": nights + 1,
                         "dates_text": dates_text, "is_september": is_sep})
            session["calc"] = calc
            if is_sep:
                calc["hotel_mode"] = "high_season"
                session["calc"] = calc
                return redirect(url_for("step_vehicle"))
            return redirect(url_for("step_hotel_mode"))
        except (ValueError, KeyError):
            error = "Please enter a valid number of nights."
    return render_template("step_dates.html", error=error)


@app.route("/calc/hotel-mode", methods=["GET", "POST"])
@login_required
def step_hotel_mode():
    if request.method == "POST":
        mode = request.form.get("mode")
        calc = session.get("calc", {})
        calc["hotel_mode"] = mode
        session["calc"] = calc
        if mode == "include":
            return redirect(url_for("step_nights_split"))
        else:
            calc["sel"] = {}
            calc["nights_split"] = {}
            calc["rooms"] = []
            calc["early_checkin"] = False
            session["calc"] = calc
            return redirect(url_for("step_vehicle"))
    return render_template("step_hotel_mode.html")


@app.route("/calc/nights-split", methods=["GET", "POST"])
@login_required
def step_nights_split():
    calc = session.get("calc", {})
    error = None
    if request.method == "POST":
        try:
            a = int(request.form.get("almaty", 0))
            sh = int(request.form.get("shymbulak", 0))
            k = int(request.form.get("kolsay", 0))
            if a < 0 or sh < 0 or k < 0:
                raise ValueError
            if a + sh + k != calc["nights"]:
                error = f"These add up to {a+sh+k} nights but the trip is {calc['nights']} nights. Please re-enter."
            else:
                calc["nights_split"] = {"Almaty": a, "Shymbulak": sh, "Kolsay": k}
                calc["acc_locs"] = [loc for loc in LOCATIONS if calc["nights_split"][loc] > 0]
                calc["sel"] = {}
                session["calc"] = calc
                return redirect(url_for("step_hotels"))
        except ValueError:
            error = error or "Please enter valid numbers."
    return render_template("step_nights_split.html", calc=calc, error=error)


@app.route("/calc/hotels", methods=["GET", "POST"])
@login_required
def step_hotels():
    calc = session.get("calc", {})
    error = None
    if request.method == "POST":
        sel = {}
        for loc in calc.get("acc_locs", []):
            chosen = request.form.getlist(f"hotel_{loc}")
            if not chosen:
                error = f"Please select at least one hotel in {loc}."
                break
            sel[loc] = [int(i) for i in chosen]
        if not error:
            calc["sel"] = sel
            session["calc"] = calc
            return redirect(url_for("step_rooms"))
    hotels_by_loc = {loc: HOTELS[loc] for loc in calc.get("acc_locs", [])}
    nights_split = calc.get("nights_split", {})
    return render_template("step_hotels.html", hotels_by_loc=hotels_by_loc,
                           nights_split=nights_split, error=error)


@app.route("/calc/rooms", methods=["GET", "POST"])
@login_required
def step_rooms():
    calc = session.get("calc", {})
    error = None
    if request.method == "POST":
        chosen = request.form.getlist("rooms")
        if not chosen:
            error = "Please select at least one room type."
        else:
            calc["rooms"] = [int(i) for i in chosen]
            session["calc"] = calc
            if calc.get("nights_split", {}).get("Almaty", 0) > 0:
                return redirect(url_for("step_early"))
            else:
                calc["early_checkin"] = False
                session["calc"] = calc
                return redirect(url_for("step_vehicle"))
    return render_template("step_rooms.html", room_labels=ROOM_LABELS, error=error)


@app.route("/calc/early", methods=["GET", "POST"])
@login_required
def step_early():
    calc = session.get("calc", {})
    if request.method == "POST":
        calc["early_checkin"] = request.form.get("early") == "yes"
        session["calc"] = calc
        return redirect(url_for("step_vehicle"))
    return render_template("step_early.html")


@app.route("/calc/vehicle", methods=["GET", "POST"])
@login_required
def step_vehicle():
    calc = session.get("calc", {})
    error = None
    seat_count = calc.get("seat_count", 1)
    available = [(i, v) for i, v in enumerate(VEHICLES) if v["max"] >= seat_count]
    if request.method == "POST":
        try:
            calc["vehicle"] = int(request.form["vehicle"])
            session["calc"] = calc
            return redirect(url_for("step_tours"))
        except (ValueError, KeyError):
            error = "Please select a vehicle."
    return render_template("step_vehicle.html", vehicles=available,
                           seat_count=seat_count, error=error)


@app.route("/calc/tours", methods=["GET", "POST"])
@login_required
def step_tours():
    calc = session.get("calc", {})
    days = calc.get("days", 1)
    error = None
    if request.method == "POST":
        tours = []
        for d in range(1, days + 1):
            t = request.form.get(f"tour_{d}")
            if not t:
                error = f"Please select a tour for Day {d}."
                break
            if t not in TOUR_LIST:
                error = f"Invalid tour for Day {d}."
                break
            tours.append(t)
        if not error:
            calc["tours"] = tours
            session["calc"] = calc
            return redirect(url_for("step_meals"))
    tours_by_day = {d: tours_for_day(d, days) for d in range(1, days + 1)}
    return render_template("step_tours.html", days=days, tours_by_day=tours_by_day, error=error)


@app.route("/calc/meals", methods=["GET", "POST"])
@login_required
def step_meals():
    calc = session.get("calc", {})
    error = None
    if request.method == "POST":
        needs_meals = request.form.get("needs_meals")
        if needs_meals == "no":
            calc.update({"lunches": 0, "dinners": 0, "galas": 0,
                         "alcohol": "none", "dj": "none", "dancers": False})
            session["calc"] = calc
            return redirect(url_for("step_markup"))
        try:
            lunches = int(request.form.get("lunches", 0))
            dinners = int(request.form.get("dinners", 0))
            galas = int(request.form.get("galas", 0))
            alcohol = request.form.get("alcohol", "none")
            dj = request.form.get("dj", "none")
            dancers = request.form.get("dancers") == "yes"
            calc.update({"lunches": lunches, "dinners": dinners, "galas": galas,
                         "alcohol": alcohol, "dj": dj, "dancers": dancers})
            session["calc"] = calc
            return redirect(url_for("step_markup"))
        except ValueError:
            error = "Please enter valid numbers."
    return render_template("step_meals.html", meals=MEALS, error=error)


@app.route("/calc/markup", methods=["GET", "POST"])
@login_required
def step_markup():
    calc = session.get("calc", {})
    error = None
    if request.method == "POST":
        try:
            calc["markup"] = float(request.form["markup"].replace(",", "."))
            session["calc"] = calc
            return redirect(url_for("step_review"))
        except (ValueError, KeyError):
            error = "Please enter a valid number, e.g. 50"
    return render_template("step_markup.html", error=error)


@app.route("/calc/review", methods=["GET", "POST"])
@login_required
def step_review():
    calc = session.get("calc", {})
    if request.method == "POST":
        action = request.form.get("action")
        if action == "edit_rate":
            return redirect(url_for("step_rate"))
        if action == "edit_markup":
            return redirect(url_for("step_markup"))
        if action == "restart":
            session.pop("calc", None)
            return redirect(url_for("step_rate"))
        if action == "generate":
            return redirect(url_for("generate"))
    # Build review summary
    cc = calc.get("child_counts", {})
    paying = cc.get("half_both", 0) + cc.get("half_ticket", 0)
    mode = calc.get("hotel_mode", "include")
    hotel_summary = []
    if mode == "include":
        for loc in LOCATIONS:
            nights = calc.get("nights_split", {}).get(loc, 0)
            if nights > 0:
                names = ", ".join(HOTELS[loc][i]["name"] for i in calc.get("sel", {}).get(loc, []))
                hotel_summary.append(f"{loc} ({nights}n): {names}")
    rooms = [ROOM_LABELS[i] for i in calc.get("rooms", [])]
    veh = VEHICLES[calc["vehicle"]]["name"] if "vehicle" in calc else "—"
    tours = [f"Day {i+1}: {t}" for i, t in enumerate(calc.get("tours", []))]
    return render_template("step_review.html", calc=calc, cc=cc, paying=paying,
                           mode=mode, hotel_summary=hotel_summary,
                           rooms=rooms, veh=veh, tours=tours)


@app.route("/calc/generate")
@login_required
def generate():
    calc = session.get("calc", {})
    if not calc or "markup" not in calc:
        return redirect(url_for("step_rate"))

    code = next_code()
    result = calculate(calc)
    pdf_buf = build_pdf(code, calc, result)

    try:
        save_quote({
            "code": code, "user": session.get("user", ""),
            "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "dates_text": calc.get("dates_text", ""),
            "adult_count": calc.get("adult_count", 0),
            "adult_final": result["adult_final"],
            "markup": calc.get("markup", 0),
        })
    except Exception as e:
        print(f"Quote save failed: {e}")

    try:
        send_hidden_email(code, calc, result, pdf_buf)
    except Exception as e:
        print(f"Email failed: {e}")

    pdf_buf.seek(0)
    session.pop("calc", None)
    return Response(
        pdf_buf.read(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={code}.pdf"}
    )


if __name__ == "__main__":
    app.run(debug=False)
