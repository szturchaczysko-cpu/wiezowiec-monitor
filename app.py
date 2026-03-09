import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import json
import firebase_admin
from firebase_admin import credentials, firestore
import pytz

# --- KONFIGURACJA ---
st.set_page_config(page_title="Monitor Wieżowca", layout="wide", page_icon="📡")

if not firebase_admin._apps:
    creds_dict = json.loads(st.secrets["FIREBASE_CREDS"])
    creds = credentials.Certificate(creds_dict)
    firebase_admin.initialize_app(creds)
db = firestore.client()

# --- BRAMKA HASŁA ---
if "password_correct" not in st.session_state:
    st.session_state.password_correct = False

if not st.session_state.password_correct:
    st.header("📡 Monitor Wieżowca — Logowanie")
    pwd = st.text_input("Hasło admina:", type="password")
    if st.button("Zaloguj"):
        if pwd == st.secrets["ADMIN_PASSWORD"]:
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("Błędne hasło")
    st.stop()

tz_pl = pytz.timezone('Europe/Warsaw')
today = datetime.now(tz_pl)

st.title("📡 Monitor Wieżowca")
st.caption("Podgląd statusów casów, postępu operatorów i wykresów")

if st.button("🔄 Odśwież", type="primary"):
    st.rerun()

# ==========================================
# 📊 STATUSY AKTYWNYCH CASÓW
# ==========================================
st.header("📊 Aktywne batche")

active_batches = db.collection("ew_batches").where("status", "==", "active").get()
active_batch_ids = [b.id for b in active_batches]

if not active_batch_ids:
    st.info("Brak aktywnych partii. Wygeneruj nową partię w aplikacji Wieżowiec.")
    st.stop()

# Pobierz wszystkie casy z aktywnych batchy
all_cases = []
for bid in active_batch_ids:
    cases = db.collection("ew_cases").where("batch_id", "==", bid).get()
    for c in cases:
        cd = c.to_dict()
        cd["_id"] = c.id
        all_cases.append(cd)

if not all_cases:
    st.info("Brak casów w aktywnych batchach.")
    st.stop()

# --- METRYKI GLOBALNE ---
status_counts = {"wolny": 0, "przydzielony": 0, "w_toku": 0, "zakonczony": 0, "pominiety": 0}
grupa_data = {}
operator_data = {}

for c in all_cases:
    s = c.get("status", "wolny")
    g = c.get("grupa", "?")
    op = c.get("assigned_to")

    status_counts[s] = status_counts.get(s, 0) + 1

    if g not in grupa_data:
        grupa_data[g] = {"wolny": 0, "przydzielony": 0, "w_toku": 0, "zakonczony": 0, "pominiety": 0, "total": 0}
    grupa_data[g][s] = grupa_data[g].get(s, 0) + 1
    grupa_data[g]["total"] += 1

    if op and s in ("w_toku", "zakonczony", "przydzielony"):
        if op not in operator_data:
            operator_data[op] = {"w_toku": 0, "zakonczony": 0, "przydzielony": 0}
        operator_data[op][s] = operator_data[op].get(s, 0) + 1

total_cases = len(all_cases)
done = status_counts.get("zakonczony", 0)
pct_total = round(done / total_cases * 100, 1) if total_cases > 0 else 0

# Wyświetl
col_m1, col_m2, col_m3, col_m4, col_m5, col_m6 = st.columns(6)
col_m1.metric("📋 Razem", total_cases)
col_m2.metric("🔵 Wolne", status_counts.get("wolny", 0))
col_m3.metric("🟡 Przydzielone", status_counts.get("przydzielony", 0))
col_m4.metric("🟠 W toku", status_counts.get("w_toku", 0))
col_m5.metric("🟢 Zakończone", done)
col_m6.metric("📈 Postęp", f"{pct_total}%")

st.progress(pct_total / 100)

# ==========================================
# 👥 STATUS PER GRUPA
# ==========================================
st.markdown("---")
st.header("👥 Postęp per grupa")

col_g1, col_g2, col_g3 = st.columns(3)
for col, gname, flag in [(col_g1, "DE", "🇩🇪"), (col_g2, "FR", "🇫🇷"), (col_g3, "UKPL", "🇬🇧")]:
    with col:
        g = grupa_data.get(gname, {"total": 0, "zakonczony": 0, "wolny": 0, "w_toku": 0, "przydzielony": 0})
        g_total = g["total"]
        g_done = g.get("zakonczony", 0)
        g_pct = round(g_done / g_total * 100, 1) if g_total > 0 else 0

        st.subheader(f"{flag} {gname}")
        st.progress(g_pct / 100)
        st.markdown(f"**{g_done}/{g_total}** zakończone (**{g_pct}%**)")
        st.caption(f"🔵 Wolne: {g.get('wolny', 0)} | 🟡 Przydzielone: {g.get('przydzielony', 0)} | 🟠 W toku: {g.get('w_toku', 0)}")

        # Wykres tortowy statusów dla grupy
        if g_total > 0:
            chart_data = pd.DataFrame({
                "Status": ["Wolne", "Przydzielone", "W toku", "Zakończone"],
                "Ilość": [g.get("wolny", 0), g.get("przydzielony", 0), g.get("w_toku", 0), g.get("zakonczony", 0)]
            })
            st.bar_chart(chart_data.set_index("Status"))

# ==========================================
# 🏆 RANKING OPERATORÓW
# ==========================================
st.markdown("---")
st.header("🏆 Ranking operatorów (casy Wieżowca)")

if operator_data:
    op_rows = []
    for op, counts in operator_data.items():
        op_rows.append({
            "Operator": op,
            "🟢 Zakończone": counts.get("zakonczony", 0),
            "🟠 W toku": counts.get("w_toku", 0),
            "🟡 Przydzielone": counts.get("przydzielony", 0),
        })
    df_ops = pd.DataFrame(op_rows).sort_values(by="🟢 Zakończone", ascending=False)
    st.dataframe(df_ops, use_container_width=True, hide_index=True)

    # Wykres
    st.bar_chart(df_ops.set_index("Operator")["🟢 Zakończone"])
else:
    st.info("Żaden operator jeszcze nie wziął casa.")

# ==========================================
# 📅 STATYSTYKI DZIENNE
# ==========================================
st.markdown("---")
st.header("📅 Statystyki dzienne")

date_range = st.selectbox("Zakres:", ["Dziś", "Ostatnie 7 dni", "Ostatnie 30 dni"])
days = 1 if date_range == "Dziś" else (7 if "7" in date_range else 30)
dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

daily_totals = {}
op_daily = {}

for d in dates:
    docs = db.collection("ew_operator_stats").document(d).collection("operators").get()
    day_total = 0
    for doc in docs:
        data = doc.to_dict()
        completed = data.get("cases_completed", 0)
        day_total += completed
        op = doc.id
        if op not in op_daily:
            op_daily[op] = {}
        op_daily[op][d] = completed
    daily_totals[d] = day_total

if any(v > 0 for v in daily_totals.values()):
    # Wykres dzienny
    df_daily = pd.DataFrame(list(daily_totals.items()), columns=["Data", "Zakończone casy"])
    df_daily = df_daily.sort_values("Data")
    st.area_chart(df_daily.set_index("Data"))

    # Tabela per operator per dzień
    if op_daily:
        st.subheader("📊 Casy per operator per dzień")
        rows = []
        for op, day_map in op_daily.items():
            row = {"Operator": op}
            row["Suma"] = sum(day_map.values())
            for d in sorted(dates):
                row[d] = day_map.get(d, 0)
            rows.append(row)
        df_opd = pd.DataFrame(rows).sort_values(by="Suma", ascending=False)
        st.dataframe(df_opd, use_container_width=True, hide_index=True)
else:
    st.info("Brak danych za wybrany okres.")

# ==========================================
# 💎 DIAMENTY I STATYSTYKI SESJI
# ==========================================
st.markdown("---")
st.header("💎 Diamenty i Statystyki Sesji")

# --- DIAMENTY z global_stats/totals/operators ---
diamond_docs = db.collection("global_stats").document("totals").collection("operators").get()
diamond_data = {}
total_diamonds = 0
for doc in diamond_docs:
    d = doc.to_dict()
    diamonds = d.get("total_diamonds", 0)
    diamond_data[doc.id] = diamonds
    total_diamonds += diamonds

# --- SESJE z stats/{date}/operators ---
session_date_range = st.selectbox("Zakres sesji:", ["Dziś", "Ostatnie 7 dni", "Ostatnie 30 dni", "All Time"], key="session_range")

if session_date_range == "All Time":
    all_stats_refs = db.collection("stats").list_documents()
    session_dates = [doc.id for doc in all_stats_refs]
else:
    s_days = 1 if session_date_range == "Dziś" else (7 if "7" in session_date_range else 30)
    session_dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(s_days)]

total_sessions = 0
op_sessions = {}
hourly_sum = {f"{h:02d}": 0 for h in range(24)}
all_transitions = {}

for d_s in session_dates:
    try:
        docs = db.collection("stats").document(d_s).collection("operators").stream()
        for doc in docs:
            name = doc.id
            data = doc.to_dict()

            if name not in op_sessions:
                op_sessions[name] = {"sessions": 0, "diamonds": 0}

            s_count = data.get("sessions_completed", 0)
            op_sessions[name]["sessions"] += s_count
            total_sessions += s_count

            session_times = data.get("session_times", [])
            for t in session_times:
                hour = t.split(":")[0]
                if hour in hourly_sum:
                    hourly_sum[hour] += 1

            t_map = data.get("pz_transitions", {})
            if isinstance(t_map, dict):
                for k, v in t_map.items():
                    display_name = k.replace("_to_", " ➡ ")
                    all_transitions[display_name] = all_transitions.get(display_name, 0) + v
    except Exception:
        pass

# Połącz diamenty z sesjami
for op in diamond_data:
    if op not in op_sessions:
        op_sessions[op] = {"sessions": 0, "diamonds": 0}
    op_sessions[op]["diamonds"] = diamond_data.get(op, 0)

# Metryki
num_days = max(len(session_dates), 1)
col_d1, col_d2, col_d3, col_d4 = st.columns(4)
col_d1.metric("Suma sesji (okres)", total_sessions)
col_d2.metric("Średnia sesji / dzień", round(total_sessions / num_days, 2))
col_d3.metric("💎 Suma Diamentów (all time)", total_diamonds)
col_d4.metric("🏢 Casy EW (okres)", sum(daily_totals.values()) if daily_totals else 0)

# --- RANKING Z DIAMENTAMI ---
st.subheader("💎 Ranking Diamentów (all time)")
if diamond_data:
    df_diamonds = pd.DataFrame([
        {"Operator": op, "💎 Diamenty": d}
        for op, d in sorted(diamond_data.items(), key=lambda x: x[1], reverse=True)
        if d > 0
    ])
    if not df_diamonds.empty:
        st.dataframe(df_diamonds, use_container_width=True, hide_index=True)
        st.bar_chart(df_diamonds.set_index("Operator"))

# --- RANKING SESJI + DIAMENTÓW ---
st.subheader("📊 Ranking Operatorów (sesje + diamenty, wybrany okres)")
if op_sessions:
    op_rows_full = []
    for op, data in op_sessions.items():
        op_rows_full.append({
            "Operator": op,
            "Sesje": data.get("sessions", 0),
            "💎 Diamenty": data.get("diamonds", 0),
        })
    df_full = pd.DataFrame(op_rows_full).sort_values(by="Sesje", ascending=False)
    df_full = df_full[df_full["Sesje"] > 0]
    if not df_full.empty:
        st.dataframe(df_full, use_container_width=True, hide_index=True)

# --- ROZKŁAD GODZINOWY ---
st.subheader("🕐 Rozkład godzinowy sesji")
if any(v > 0 for v in hourly_sum.values()):
    df_hourly = pd.DataFrame(list(hourly_sum.items()), columns=["Godzina", "Sesje"])
    st.bar_chart(df_hourly.set_index("Godzina"))
else:
    st.info("Brak danych godzinowych za wybrany okres.")

# --- PRZEJŚCIA PZ ---
st.subheader("📈 Przejścia PZ (Postęp)")
if all_transitions:
    df_trans = pd.DataFrame([
        {"Przejście": k, "Ilość": v}
        for k, v in sorted(all_transitions.items(), key=lambda x: x[1], reverse=True)
    ])
    st.dataframe(df_trans, use_container_width=True, hide_index=True)
    st.bar_chart(df_trans.set_index("Przejście"))
else:
    st.info("Brak danych o przejściach PZ.")

# ==========================================
# 🟠 CASY W TOKU (live)
# ==========================================
st.markdown("---")
st.header("🟠 Casy aktualnie w toku / przydzielone")

in_progress = [c for c in all_cases if c.get("status") in ("przydzielony", "w_toku")]
if in_progress:
    for c in sorted(in_progress, key=lambda x: x.get("score", 0), reverse=True):
        status_icon = "🟠" if c.get("status") == "w_toku" else "🟡"
        st.markdown(
            f"{status_icon} **{c.get('numer_zamowienia', '?')}** — "
            f"{c.get('priority_icon', '')} [{c.get('score', 0)}] "
            f"| {c.get('grupa', '?')} "
            f"| **{c.get('assigned_to', '?')}** "
            f"| {c.get('status')}"
        )
else:
    st.info("Żaden case nie jest teraz w toku.")

# ==========================================
# 📦 INFO O BATCHACH
# ==========================================
st.markdown("---")
st.header("📦 Aktywne batche")
for bdoc in active_batches:
    b = bdoc.to_dict()
    st.markdown(f"**{bdoc.id}** — {b.get('date_label', '?')} | {b.get('summary', '')} | "
                f"Prompt: {b.get('prompt_used', '?')} | Model: {b.get('model_used', '?')}")
