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

# ==========================================
# 🧪 TOGGLE ŚRODOWISKA
# ==========================================
# Apka operatorów (app_vertex_ew.py) ma TEST_MODE=True → pisze do kolekcji z prefixem "test_".
# Mimo nazwy "test" to są AKTUALNE dane biznesowe.
# Stara produkcja (bez prefixu) = archiwum historyczne.
if "monitor_use_test_prefix" not in st.session_state:
    st.session_state.monitor_use_test_prefix = True

with st.sidebar:
    st.markdown("### 🗂️ Środowisko")
    env_choice = st.radio(
        "Źródło danych:",
        options=["test_ (aktualne)", "(bez prefixu) — archiwum"],
        index=0 if st.session_state.monitor_use_test_prefix else 1,
        key="_env_radio",
        help="Apka operatorów pisze do test_*. Bez prefixu = stare dane prod."
    )
    st.session_state.monitor_use_test_prefix = env_choice.startswith("test_")
    st.markdown("---")
    st.caption("💡 Operator_configs zawsze z produkcji (bez prefixu).")

_COL_PREFIX = "test_" if st.session_state.monitor_use_test_prefix else ""

def col(name):
    return f"{_COL_PREFIX}{name}"

tz_pl = pytz.timezone('Europe/Warsaw')
today = datetime.now(tz_pl)
today_str = today.strftime("%Y-%m-%d")

# ==========================================
# 👥 MAPOWANIE OPERATOR → GRUPA
# ==========================================
ROLE_TO_GRUPA = {
    "Operatorzy_DE": "DE",
    "Operatorzy_FR": "FR",
    "Operatorzy_UK/PL": "UKPL",
}

@st.cache_data(ttl=120)
def get_op_to_grupa():
    """operator_configs zawsze z produkcji (bez prefixu) — identycznie jak app_vertex_ew."""
    docs = db.collection("operator_configs").get()
    result = {}
    for d in docs:
        cd = d.to_dict() or {}
        role = cd.get("role", "")
        result[d.id] = ROLE_TO_GRUPA.get(role, "?")
    return result

op_to_grupa = get_op_to_grupa()

# ==========================================
# 🔧 HELPERY: pobieranie danych
# ==========================================
def fetch_stats_for_date(date_str):
    """Zwraca {operator: {pz_transitions: {...}, sessions_completed: N, ...}} dla danego dnia."""
    docs = db.collection(col("stats")).document(date_str).collection("operators").get()
    return {d.id: (d.to_dict() or {}) for d in docs}

def fetch_ew_stats_for_date(date_str):
    """Zwraca {operator: {cases_completed: N, cases_taken: N, cases_skipped: N}}."""
    docs = db.collection(col("ew_operator_stats")).document(date_str).collection("operators").get()
    return {d.id: (d.to_dict() or {}) for d in docs}

def count_pz6_for_operator(stats_data):
    """Sumuje wszystkie pz_transitions.*_to_PZ6 z dokumentu statystyk operatora."""
    return sum(v for k, v in stats_data.get("pz_transitions", {}).items() if k.endswith("_to_PZ6"))

def fetch_global_diamonds():
    """Zwraca {operator: total_diamonds} z global_stats/totals/operators."""
    docs = db.collection(col("global_stats")).document("totals").collection("operators").get()
    return {d.id: (d.to_dict() or {}).get("total_diamonds", 0) for d in docs}

# ==========================================
# 🏠 HEADER
# ==========================================
env_label = "🧪 test_" if st.session_state.monitor_use_test_prefix else "🏭 prod"
st.title("📡 Monitor Wieżowca")
st.caption(f"Środowisko: **{env_label}** | Dziś: {today_str} | Operatorów w konfiguracji: {len(op_to_grupa)}")

col_btn1, col_btn2 = st.columns([1, 5])
with col_btn1:
    if st.button("🔄 Odśwież", type="primary"):
        st.cache_data.clear()
        st.rerun()

# ==========================================
# 📊 AKTYWNE BATCHE (top-line)
# ==========================================
st.markdown("---")
st.header("📊 Aktywne batche")

active_batches = list(db.collection(col("ew_batches")).where("status", "==", "active").get())
active_batch_ids = [b.id for b in active_batches]

all_cases = []
for bid in active_batch_ids:
    cases = db.collection(col("ew_cases")).where("batch_id", "==", bid).get()
    for c in cases:
        cd = c.to_dict()
        cd["_id"] = c.id
        all_cases.append(cd)

if not active_batch_ids:
    st.info("Brak aktywnych partii. Diamenty i historia działają niezależnie — przewiń niżej.")
elif not all_cases:
    st.warning("Aktywne batche są, ale brak casów w nich.")
else:
    status_counts = {"wolny": 0, "przydzielony": 0, "w_toku": 0, "zakonczony": 0, "pominiety": 0}
    grupa_data = {}

    for c in all_cases:
        s = c.get("status", "wolny")
        g = c.get("grupa", "?")
        status_counts[s] = status_counts.get(s, 0) + 1
        if g not in grupa_data:
            grupa_data[g] = {"wolny": 0, "przydzielony": 0, "w_toku": 0, "zakonczony": 0, "pominiety": 0, "total": 0}
        grupa_data[g][s] = grupa_data[g].get(s, 0) + 1
        grupa_data[g]["total"] += 1

    total_cases = len(all_cases)
    done = status_counts.get("zakonczony", 0)
    pct_total = round(done / total_cases * 100, 1) if total_cases > 0 else 0

    col_m1, col_m2, col_m3, col_m4, col_m5, col_m6 = st.columns(6)
    col_m1.metric("📋 Razem", total_cases)
    col_m2.metric("🔵 Wolne", status_counts.get("wolny", 0))
    col_m3.metric("🟡 Przydzielone", status_counts.get("przydzielony", 0))
    col_m4.metric("🟠 W toku", status_counts.get("w_toku", 0))
    col_m5.metric("🟢 Zakończone", done)
    col_m6.metric("📈 Postęp", f"{pct_total}%")
    st.progress(pct_total / 100)

    # POSTĘP PER GRUPA
    st.subheader("👥 Postęp per grupa (aktywne batche)")
    col_g1, col_g2, col_g3 = st.columns(3)
    for gcol, gname, flag in [(col_g1, "DE", "🇩🇪"), (col_g2, "FR", "🇫🇷"), (col_g3, "UKPL", "🇬🇧")]:
        with gcol:
            g = grupa_data.get(gname, {"total": 0, "zakonczony": 0, "wolny": 0, "w_toku": 0, "przydzielony": 0})
            g_total = g["total"]
            g_done = g.get("zakonczony", 0)
            g_pct = round(g_done / g_total * 100, 1) if g_total > 0 else 0
            st.markdown(f"**{flag} {gname}**")
            st.progress(g_pct / 100)
            st.markdown(f"**{g_done}/{g_total}** zakończone (**{g_pct}%**)")
            st.caption(f"🔵 {g.get('wolny', 0)} | 🟡 {g.get('przydzielony', 0)} | 🟠 {g.get('w_toku', 0)}")

# ==========================================
# 💎 DIAMENTY
# ==========================================
st.markdown("---")
st.header("💎 Diamenty (zamówieni kurierzy = PZ6)")

# --- All-time ---
global_diamonds = fetch_global_diamonds()
total_all_time = sum(global_diamonds.values())

# --- Dziś ---
stats_today = fetch_stats_for_date(today_str)
today_diamonds_by_op = {op: count_pz6_for_operator(data) for op, data in stats_today.items()}
total_today = sum(today_diamonds_by_op.values())

# --- Per grupa (all-time + dziś) ---
diamonds_per_grupa_alltime = {"DE": 0, "FR": 0, "UKPL": 0, "?": 0}
diamonds_per_grupa_today = {"DE": 0, "FR": 0, "UKPL": 0, "?": 0}

all_ops_with_diamonds = set(global_diamonds.keys()) | set(today_diamonds_by_op.keys())
for op in all_ops_with_diamonds:
    grupa = op_to_grupa.get(op, "?")
    diamonds_per_grupa_alltime[grupa] = diamonds_per_grupa_alltime.get(grupa, 0) + global_diamonds.get(op, 0)
    diamonds_per_grupa_today[grupa] = diamonds_per_grupa_today.get(grupa, 0) + today_diamonds_by_op.get(op, 0)

# --- Metryki globalne ---
col_d1, col_d2, col_d3, col_d4 = st.columns(4)
col_d1.metric("💎 Łącznie (all-time)", total_all_time)
col_d2.metric("💎 Dziś (suma)", total_today)
col_d3.metric("👥 Operatorów z diamentami", sum(1 for v in global_diamonds.values() if v > 0))
col_d4.metric("📅 Data", today_str)

# --- Breakdown per grupa ---
st.subheader("💎 Diamenty per grupa")
col_dg1, col_dg2, col_dg3 = st.columns(3)
for dgcol, gname, flag in [(col_dg1, "DE", "🇩🇪"), (col_dg2, "FR", "🇫🇷"), (col_dg3, "UKPL", "🇬🇧")]:
    with dgcol:
        st.markdown(f"**{flag} {gname}**")
        st.metric("All-time", diamonds_per_grupa_alltime.get(gname, 0))
        st.metric("Dziś", diamonds_per_grupa_today.get(gname, 0))

if diamonds_per_grupa_alltime.get("?", 0) > 0:
    st.caption(f"⚠️ {diamonds_per_grupa_alltime['?']} diamentów u operatorów bez przypisanej grupy (sprawdź operator_configs).")

# --- Ranking diamentów ---
st.subheader("🏆 Ranking diamentów")
rows = []
for op in sorted(all_ops_with_diamonds):
    alltime = global_diamonds.get(op, 0)
    todayd = today_diamonds_by_op.get(op, 0)
    if alltime == 0 and todayd == 0:
        continue
    rows.append({
        "Operator": op,
        "Grupa": op_to_grupa.get(op, "?"),
        "💎 All-time": alltime,
        "💎 Dziś": todayd,
    })

if rows:
    df_dia = pd.DataFrame(rows).sort_values(by="💎 All-time", ascending=False)
    st.dataframe(df_dia, use_container_width=True, hide_index=True)

    # Wykres top 10 all-time
    df_top = df_dia.head(10)
    st.bar_chart(df_top.set_index("Operator")[["💎 All-time"]])
else:
    st.info("Brak diamentów w wybranym środowisku.")

# --- Wykres dzienny diamentów w zakresie ---
st.subheader("📈 Diamenty dzień po dniu (zakres)")
col_dr1, col_dr2 = st.columns(2)
with col_dr1:
    dia_from = st.date_input("Od:", value=today.date() - timedelta(days=13), key="dia_from")
with col_dr2:
    dia_to = st.date_input("Do:", value=today.date(), key="dia_to")

dia_days = (dia_to - dia_from).days + 1
if dia_days < 1:
    st.error("Data 'Od' musi być przed lub równa 'Do'.")
else:
    dia_dates = [(dia_from + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(dia_days)]
    daily_diamonds = {}
    per_op_per_day = {}
    for d in dia_dates:
        sdata = fetch_stats_for_date(d)
        day_sum = 0
        for op, opdata in sdata.items():
            pz6 = count_pz6_for_operator(opdata)
            day_sum += pz6
            if pz6 > 0:
                per_op_per_day.setdefault(op, {})[d] = pz6
        daily_diamonds[d] = day_sum

    period_total = sum(daily_diamonds.values())
    st.metric(f"💎 W okresie ({dia_days} dni)", period_total)

    if period_total > 0:
        df_daily_dia = pd.DataFrame({
            "Data": list(daily_diamonds.keys()),
            "💎 Diamenty": list(daily_diamonds.values()),
        }).sort_values("Data")
        st.area_chart(df_daily_dia.set_index("Data"))

        # Tabela per operator
        if per_op_per_day:
            rows_pod = []
            for op, daymap in per_op_per_day.items():
                row = {
                    "Operator": op,
                    "Grupa": op_to_grupa.get(op, "?"),
                    "💎 Suma okresu": sum(daymap.values()),
                }
                for d in sorted(dia_dates):
                    row[d] = daymap.get(d, 0)
                rows_pod.append(row)
            df_pod = pd.DataFrame(rows_pod).sort_values(by="💎 Suma okresu", ascending=False)
            st.dataframe(df_pod, use_container_width=True, hide_index=True)
    else:
        st.info("Brak diamentów w wybranym okresie.")

# ==========================================
# 🏆 RANKING OPERATORÓW (wszyscy z global_stats)
# ==========================================
st.markdown("---")
st.header("🏆 Ranking operatorów (kompletny)")

ew_today_stats = fetch_ew_stats_for_date(today_str)

# Operatorzy z ew_cases (aktywne batche) — kto ma w toku/przydzielone
active_per_op = {}
for c in all_cases:
    op = c.get("assigned_to")
    s = c.get("status")
    if op and s in ("przydzielony", "w_toku"):
        active_per_op[op] = active_per_op.get(op, 0) + 1

# Łączymy wszystkich znanych operatorów
all_known_ops = set(global_diamonds.keys()) | set(ew_today_stats.keys()) | set(active_per_op.keys()) | set(op_to_grupa.keys())

rank_rows = []
for op in sorted(all_known_ops):
    ews = ew_today_stats.get(op, {})
    rank_rows.append({
        "Operator": op,
        "Grupa": op_to_grupa.get(op, "?"),
        "💎 All-time": global_diamonds.get(op, 0),
        "💎 Dziś": today_diamonds_by_op.get(op, 0),
        "✅ Zakończone dziś": ews.get("cases_completed", 0),
        "📥 Pobrane dziś": ews.get("cases_taken", 0),
        "⏭️ Pominięte dziś": ews.get("cases_skipped", 0),
        "🏢 Aktywne casy": active_per_op.get(op, 0),
    })

if rank_rows:
    df_rank = pd.DataFrame(rank_rows).sort_values(by=["💎 All-time", "✅ Zakończone dziś"], ascending=[False, False])
    st.dataframe(df_rank, use_container_width=True, hide_index=True)
else:
    st.info("Brak operatorów w danych.")

# ==========================================
# 📅 STATYSTYKI DZIENNE EW (cases_completed/taken/skipped)
# ==========================================
st.markdown("---")
st.header("📅 Statystyki dzienne (casy Wieżowca)")

col_date1, col_date2 = st.columns(2)
with col_date1:
    ew_date_from = st.date_input("Od:", value=today.date() - timedelta(days=6), key="ew_from")
with col_date2:
    ew_date_to = st.date_input("Do:", value=today.date(), key="ew_to")

ew_days_n = (ew_date_to - ew_date_from).days + 1
if ew_days_n < 1:
    st.error("Data 'Od' musi być przed lub równa 'Do'.")
    dates = []
else:
    dates = [(ew_date_from + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(ew_days_n)]

daily_totals = {}
op_daily = {}

for d in dates:
    sdata = fetch_ew_stats_for_date(d)
    day_total_completed = 0
    day_total_taken = 0
    day_total_skipped = 0
    for op, data in sdata.items():
        completed = data.get("cases_completed", 0)
        taken = data.get("cases_taken", 0)
        skipped = data.get("cases_skipped", 0)
        day_total_completed += completed
        day_total_taken += taken
        day_total_skipped += skipped
        if op not in op_daily:
            op_daily[op] = {}
        op_daily[op][d] = {"taken": taken, "completed": completed, "skipped": skipped}
    daily_totals[d] = {"taken": day_total_taken, "completed": day_total_completed, "skipped": day_total_skipped}

period_taken = sum(v["taken"] for v in daily_totals.values())
period_completed = sum(v["completed"] for v in daily_totals.values())
period_skipped = sum(v["skipped"] for v in daily_totals.values())

col_ew1, col_ew2, col_ew3, col_ew4 = st.columns(4)
col_ew1.metric("📥 Pobrane (okres)", period_taken)
col_ew2.metric("✅ Zakończone (okres)", period_completed)
col_ew3.metric("⏭️ Pominięte (okres)", period_skipped)
col_ew4.metric("📅 Dni", len(dates))

if any(v["completed"] > 0 or v["taken"] > 0 for v in daily_totals.values()):
    df_daily = pd.DataFrame({
        "Data": list(daily_totals.keys()),
        "📥 Pobrane": [v["taken"] for v in daily_totals.values()],
        "✅ Zakończone": [v["completed"] for v in daily_totals.values()],
        "⏭️ Pominięte": [v["skipped"] for v in daily_totals.values()],
    }).sort_values("Data")
    st.area_chart(df_daily.set_index("Data"))

    if op_daily:
        st.subheader("📊 Operatorzy — szczegóły per dzień")
        rows = []
        for op, day_map in op_daily.items():
            row = {"Operator": op, "Grupa": op_to_grupa.get(op, "?")}
            row["📥 Pobrane"] = sum(dm.get("taken", 0) for dm in day_map.values())
            row["✅ Zakończone"] = sum(dm.get("completed", 0) for dm in day_map.values())
            row["⏭️ Pominięte"] = sum(dm.get("skipped", 0) for dm in day_map.values())
            for d in sorted(dates):
                dm = day_map.get(d, {})
                row[d] = f"{dm.get('completed', 0)}/{dm.get('taken', 0)}"
            rows.append(row)
        df_opd = pd.DataFrame(rows).sort_values(by="✅ Zakończone", ascending=False)
        st.dataframe(df_opd, use_container_width=True, hide_index=True)
        st.caption("Format kolumny daty: zakończone/pobrane")
else:
    st.info("Brak danych za wybrany okres.")

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
st.header("📦 Aktywne batche — detale")
if active_batches:
    for bdoc in active_batches:
        b = bdoc.to_dict()
        st.markdown(f"**{bdoc.id}** — {b.get('date_label', '?')} | {b.get('summary', '')} | "
                    f"Prompt: {b.get('prompt_used', '?')} | Model: {b.get('model_used', '?')}")
else:
    st.info("Brak aktywnych batchy.")

# ==========================================
# 🔧 DEBUG / SUROWE DANE
# ==========================================
st.markdown("---")
with st.expander("🔧 Debug — surowe dokumenty Firestore"):
    st.caption(f"Prefix kolekcji: `{_COL_PREFIX}` | Pełna nazwa: np. `{col('stats')}`, `{col('global_stats')}`, `{col('ew_operator_stats')}`")
    debug_op = st.selectbox("Operator do podglądu:", [""] + sorted(op_to_grupa.keys()), key="_debug_op")
    debug_date = st.date_input("Data:", value=today.date(), key="_debug_date")
    if debug_op:
        ddate = debug_date.strftime("%Y-%m-%d")
        st.markdown(f"**`{col('stats')}/{ddate}/operators/{debug_op}`** (sesje + PZ transitions):")
        stat_doc = db.collection(col("stats")).document(ddate).collection("operators").document(debug_op).get()
        st.json(stat_doc.to_dict() or {"_empty": True})

        st.markdown(f"**`{col('ew_operator_stats')}/{ddate}/operators/{debug_op}`** (casy EW dziennie):")
        ew_doc = db.collection(col("ew_operator_stats")).document(ddate).collection("operators").document(debug_op).get()
        st.json(ew_doc.to_dict() or {"_empty": True})

        st.markdown(f"**`{col('global_stats')}/totals/operators/{debug_op}`** (all-time):")
        g_doc = db.collection(col("global_stats")).document("totals").collection("operators").document(debug_op).get()
        st.json(g_doc.to_dict() or {"_empty": True})
