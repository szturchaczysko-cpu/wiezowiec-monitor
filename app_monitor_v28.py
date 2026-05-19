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
# 🗂️ ŚRODOWISKO — 3 opcje
# ==========================================
# Apka operatorów ma TEST_MODE=True → pisze do test_*. Stara prod (bez prefixu) ma historię.
# Domyślnie sumujemy oba → pełna historia + bieżące dane.
ENV_MODES = {
    "test_ + prod (suma — pełna historia)": ["test_", ""],
    "tylko test_ (aktualne ruchy)": ["test_"],
    "tylko prod / bez prefixu (archiwum)": [""],
}

with st.sidebar:
    st.markdown("### 🗂️ Źródło danych")
    env_choice = st.radio(
        "Skąd czytać:",
        options=list(ENV_MODES.keys()),
        index=0,
        key="_env_radio_v3",
        help="Apka operatorów (TEST_MODE=True) pisze do test_*. Historia może być w obu kolekcjach."
    )
    st.markdown("---")
    st.caption("💡 operator_configs zawsze bez prefixu (jak w apce).")

# Defensive: jeśli env_choice nie pasuje (np. stary session_state), fallback na sumę
active_prefixes = ENV_MODES.get(env_choice, ["test_", ""])
if not active_prefixes:
    active_prefixes = ["test_", ""]

def col_paths(name):
    """Zwraca listę pełnych nazw kolekcji dla aktywnego trybu."""
    return [f"{p}{name}" for p in active_prefixes]

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
    docs = db.collection("operator_configs").get()
    result = {}
    for d in docs:
        cd = d.to_dict() or {}
        role = cd.get("role", "")
        result[d.id] = ROLE_TO_GRUPA.get(role, "?")
    return result

op_to_grupa = get_op_to_grupa()

# ==========================================
# 🔧 HELPERY — czytają i scalają dane z aktywnych prefixów
# ==========================================
def fetch_stats_for_date(date_str):
    """Zwraca {op: {pz_transitions, sessions_completed, session_times}} z wszystkich prefixów."""
    result = {}
    for path in col_paths("stats"):
        try:
            docs = db.collection(path).document(date_str).collection("operators").get()
        except Exception:
            continue
        for d in docs:
            data = d.to_dict() or {}
            if d.id not in result:
                result[d.id] = {
                    "sessions_completed": 0,
                    "pz_transitions": {},
                    "session_times": [],
                }
            existing = result[d.id]
            existing["sessions_completed"] += data.get("sessions_completed", 0)
            for k, v in (data.get("pz_transitions", {}) or {}).items():
                existing["pz_transitions"][k] = existing["pz_transitions"].get(k, 0) + v
            existing["session_times"].extend(data.get("session_times", []) or [])
    return result

def fetch_ew_stats_for_date(date_str):
    """Zwraca {op: {cases_completed, cases_taken, cases_skipped}} z wszystkich prefixów."""
    result = {}
    for path in col_paths("ew_operator_stats"):
        try:
            docs = db.collection(path).document(date_str).collection("operators").get()
        except Exception:
            continue
        for d in docs:
            data = d.to_dict() or {}
            if d.id not in result:
                result[d.id] = {"cases_completed": 0, "cases_taken": 0, "cases_skipped": 0, "completion_times": []}
            existing = result[d.id]
            existing["cases_completed"] += data.get("cases_completed", 0)
            existing["cases_taken"] += data.get("cases_taken", 0)
            existing["cases_skipped"] += data.get("cases_skipped", 0)
            existing["completion_times"].extend(data.get("completion_times", []) or [])
    return result

def count_pz6_for_operator(stats_data):
    return sum(v for k, v in stats_data.get("pz_transitions", {}).items() if k.endswith("_to_PZ6"))

def fetch_global_diamonds():
    """Sumuje total_diamonds z wszystkich aktywnych prefixów."""
    result = {}
    for path in col_paths("global_stats"):
        try:
            docs = db.collection(path).document("totals").collection("operators").get()
        except Exception:
            continue
        for d in docs:
            val = (d.to_dict() or {}).get("total_diamonds", 0)
            result[d.id] = result.get(d.id, 0) + val
    return result

def fetch_active_batches_and_cases():
    """Zwraca (batches_list, cases_list) ze wszystkich aktywnych prefixów."""
    batches_out = []  # lista (prefix, doc)
    cases_out = []    # lista dictów (każdy z _source)
    for prefix in active_prefixes:
        batches_path = f"{prefix}ew_batches"
        cases_path = f"{prefix}ew_cases"
        try:
            batches = list(db.collection(batches_path).where("status", "==", "active").get())
        except Exception:
            batches = []
        batches_out.extend([(prefix, b) for b in batches])
        for bdoc in batches:
            try:
                cases = db.collection(cases_path).where("batch_id", "==", bdoc.id).get()
            except Exception:
                cases = []
            for c in cases:
                cd = c.to_dict() or {}
                cd["_id"] = c.id
                cd["_source_prefix"] = prefix or "(brak prefixu)"
                cases_out.append(cd)
    return batches_out, cases_out

# ==========================================
# 🏠 HEADER
# ==========================================
prefixes_label = " + ".join([p or "(prod)" for p in active_prefixes])
st.title("📡 Monitor Wieżowca")
st.caption(f"Środowisko: **{prefixes_label}** | Dziś: {today_str} | Operatorów w configu: {len(op_to_grupa)}")

col_btn1, _ = st.columns([1, 5])
with col_btn1:
    if st.button("🔄 Odśwież", type="primary"):
        st.cache_data.clear()
        st.rerun()

# ==========================================
# 🔍 PASEK DIAGNOSTYCZNY — bezwarunkowo widoczny
# ==========================================
st.markdown("---")
with st.expander("🔍 Diagnostyka źródeł danych (sprawdź gdzie naprawdę są diamenty)", expanded=True):
    st.caption("Liczby z OBU kolekcji niezależnie od wybranego trybu. Pokazuje gdzie fizycznie są dane.")

    diag_rows = []
    for prefix_check in ["test_", ""]:
        label = f"`{prefix_check}global_stats`" if prefix_check else "`global_stats` (bez prefixu)"
        try:
            gs_docs = list(db.collection(f"{prefix_check}global_stats").document("totals").collection("operators").get())
            n_ops = len(gs_docs)
            total = sum((d.to_dict() or {}).get("total_diamonds", 0) for d in gs_docs)
            top_ops = sorted(
                [(d.id, (d.to_dict() or {}).get("total_diamonds", 0)) for d in gs_docs],
                key=lambda x: x[1], reverse=True
            )[:5]
            top_str = ", ".join([f"{op}={v}" for op, v in top_ops if v > 0]) or "(brak)"
        except Exception as e:
            n_ops, total, top_str = 0, 0, f"BŁĄD: {e}"
        diag_rows.append({
            "Kolekcja": label,
            "Liczba operatorów": n_ops,
            "Suma total_diamonds": total,
            "Top 5": top_str,
        })

        # stats/today
        try:
            today_docs = list(db.collection(f"{prefix_check}stats").document(today_str).collection("operators").get())
            n_today = len(today_docs)
            total_pz6_today = 0
            for d in today_docs:
                data = d.to_dict() or {}
                total_pz6_today += sum(v for k, v in (data.get("pz_transitions", {}) or {}).items() if k.endswith("_to_PZ6"))
        except Exception:
            n_today, total_pz6_today = 0, 0

        diag_rows.append({
            "Kolekcja": f"`{prefix_check}stats/{today_str}/operators`",
            "Liczba operatorów": n_today,
            "Suma total_diamonds": f"{total_pz6_today} (PZ6 dziś)",
            "Top 5": "",
        })

    df_diag = pd.DataFrame(diag_rows)
    st.dataframe(df_diag, use_container_width=True, hide_index=True)

    # Czerwona flaga gdy aktywne źródła = 0
    total_active = 0
    for prefix in active_prefixes:
        try:
            gs = db.collection(f"{prefix}global_stats").document("totals").collection("operators").get()
            total_active += sum((d.to_dict() or {}).get("total_diamonds", 0) for d in gs)
        except Exception:
            pass
    if total_active == 0:
        st.error(f"⚠️ Aktywne źródła ({prefixes_label}) mają 0 diamentów. Zmień tryb w sidebarze albo sprawdź czy nazwa kolekcji się nie zmieniła.")
    else:
        st.success(f"✅ Aktywne źródła ({prefixes_label}) mają w sumie {total_active} diamentów all-time.")

    # --- Bonus: porównanie diagnostyki (direct query) z fetch helperami ---
    st.markdown("**🔬 Sanity check helperów (czy fetch_* widzi to samo co diagnostyka):**")
    _fgd = fetch_global_diamonds()
    _fst = fetch_stats_for_date(today_str)
    sanity_rows = [
        {
            "Co": "active_prefixes",
            "Wartość": str(active_prefixes),
        },
        {
            "Co": "col_paths('global_stats')",
            "Wartość": str(col_paths("global_stats")),
        },
        {
            "Co": "fetch_global_diamonds()",
            "Wartość": f"{len(_fgd)} operatorów, suma = {sum(_fgd.values())}",
        },
        {
            "Co": f"fetch_stats_for_date('{today_str}')",
            "Wartość": f"{len(_fst)} operatorów, PZ6 dziś = {sum(count_pz6_for_operator(d) for d in _fst.values())}",
        },
    ]
    st.dataframe(pd.DataFrame(sanity_rows), use_container_width=True, hide_index=True)
    if sum(_fgd.values()) == 0 and total_active > 0:
        st.error("🐛 BUG: diagnostyka znalazła diamenty, ale fetch_global_diamonds zwraca 0. Daj znać.")
    elif sum(_fgd.values()) > 0:
        st.success(f"✅ Helper fetch_global_diamonds widzi {sum(_fgd.values())} diamentów.")

# ==========================================
# 📑 ZAKŁADKI
# ==========================================
tab1, tab2 = st.tabs(["📊 Batche + diamenty (live)", "📈 Aktywność operatorów (zakres dat)"])

# ============================================================
# ============== ZAKŁADKA 1 ===================================
# ============================================================
with tab1:
    st.header("📊 Aktywne batche")

    active_batches_pairs, all_cases = fetch_active_batches_and_cases()
    active_batches = [b for _, b in active_batches_pairs]

    if not active_batches:
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

    # ----- DIAMENTY -----
    st.markdown("---")
    st.header("💎 Diamenty (zamówieni kurierzy = PZ6)")

    global_diamonds = fetch_global_diamonds()
    total_all_time = sum(global_diamonds.values())

    stats_today = fetch_stats_for_date(today_str)
    today_diamonds_by_op = {op: count_pz6_for_operator(data) for op, data in stats_today.items()}
    total_today = sum(today_diamonds_by_op.values())

    diamonds_per_grupa_alltime = {"DE": 0, "FR": 0, "UKPL": 0, "?": 0}
    diamonds_per_grupa_today = {"DE": 0, "FR": 0, "UKPL": 0, "?": 0}

    all_ops_with_diamonds = set(global_diamonds.keys()) | set(today_diamonds_by_op.keys())
    for op in all_ops_with_diamonds:
        grupa = op_to_grupa.get(op, "?")
        diamonds_per_grupa_alltime[grupa] = diamonds_per_grupa_alltime.get(grupa, 0) + global_diamonds.get(op, 0)
        diamonds_per_grupa_today[grupa] = diamonds_per_grupa_today.get(grupa, 0) + today_diamonds_by_op.get(op, 0)

    col_d1, col_d2, col_d3, col_d4 = st.columns(4)
    col_d1.metric("💎 Łącznie (all-time)", total_all_time)
    col_d2.metric("💎 Dziś (suma)", total_today)
    col_d3.metric("👥 Operatorów z diamentami", sum(1 for v in global_diamonds.values() if v > 0))
    col_d4.metric("📅 Data", today_str)

    st.subheader("💎 Diamenty per grupa")
    col_dg1, col_dg2, col_dg3 = st.columns(3)
    for dgcol, gname, flag in [(col_dg1, "DE", "🇩🇪"), (col_dg2, "FR", "🇫🇷"), (col_dg3, "UKPL", "🇬🇧")]:
        with dgcol:
            st.markdown(f"**{flag} {gname}**")
            st.metric("All-time", diamonds_per_grupa_alltime.get(gname, 0))
            st.metric("Dziś", diamonds_per_grupa_today.get(gname, 0))

    if diamonds_per_grupa_alltime.get("?", 0) > 0:
        st.caption(f"⚠️ {diamonds_per_grupa_alltime['?']} diamentów u operatorów bez przypisanej grupy.")

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
        df_top = df_dia.head(10)
        st.bar_chart(df_top.set_index("Operator")[["💎 All-time"]])
    else:
        st.info("Brak diamentów w wybranym środowisku. Sprawdź pasek diagnostyczny u góry.")

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

    # ----- RANKING KOMPLETNY -----
    st.markdown("---")
    st.header("🏆 Ranking operatorów (kompletny — dziś)")

    ew_today_stats = fetch_ew_stats_for_date(today_str)
    active_per_op = {}
    for c in all_cases:
        op = c.get("assigned_to")
        s = c.get("status")
        if op and s in ("przydzielony", "w_toku"):
            active_per_op[op] = active_per_op.get(op, 0) + 1

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

    # ----- CASY W TOKU -----
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

    # ----- INFO O BATCHACH -----
    st.markdown("---")
    st.header("📦 Aktywne batche — detale")
    if active_batches:
        for prefix, bdoc in active_batches_pairs:
            b = bdoc.to_dict() or {}
            src = prefix or "(prod)"
            st.markdown(f"**{bdoc.id}** *(z `{src}ew_batches`)* — {b.get('date_label', '?')} | {b.get('summary', '')} | "
                        f"Prompt: {b.get('prompt_used', '?')} | Model: {b.get('model_used', '?')}")
    else:
        st.info("Brak aktywnych batchy.")

    # ----- DEBUG -----
    st.markdown("---")
    with st.expander("🔧 Debug — surowe dokumenty Firestore (per prefix)"):
        debug_op = st.selectbox("Operator do podglądu:", [""] + sorted(op_to_grupa.keys()), key="_debug_op")
        debug_date = st.date_input("Data:", value=today.date(), key="_debug_date")
        if debug_op:
            ddate = debug_date.strftime("%Y-%m-%d")
            for prefix_check in ["test_", ""]:
                src_label = prefix_check or "(prod / bez prefixu)"
                st.markdown(f"#### 📂 Źródło: `{src_label}`")
                st.markdown(f"**`{prefix_check}stats/{ddate}/operators/{debug_op}`**:")
                stat_doc = db.collection(f"{prefix_check}stats").document(ddate).collection("operators").document(debug_op).get()
                st.json(stat_doc.to_dict() or {"_empty": True})
                st.markdown(f"**`{prefix_check}ew_operator_stats/{ddate}/operators/{debug_op}`**:")
                ew_doc = db.collection(f"{prefix_check}ew_operator_stats").document(ddate).collection("operators").document(debug_op).get()
                st.json(ew_doc.to_dict() or {"_empty": True})
                st.markdown(f"**`{prefix_check}global_stats/totals/operators/{debug_op}`**:")
                g_doc = db.collection(f"{prefix_check}global_stats").document("totals").collection("operators").document(debug_op).get()
                st.json(g_doc.to_dict() or {"_empty": True})
                st.markdown("---")

# ============================================================
# ============ ZAKŁADKA 2: aktywność per operator ============
# ============================================================
with tab2:
    st.header("📈 Aktywność operatorów w okresie")
    st.caption("Per operator (bez podziału na grupę): ile diamentów + ile sesji/casów. Wykresy przy ≤14 dni.")

    col_ad1, col_ad2 = st.columns(2)
    with col_ad1:
        act_from = st.date_input("Od:", value=today.date() - timedelta(days=6), key="act_from")
    with col_ad2:
        act_to = st.date_input("Do:", value=today.date(), key="act_to")

    days_n = (act_to - act_from).days + 1
    if days_n < 1:
        st.error("Data 'Od' musi być przed lub równa 'Do'.")
    else:
        act_dates = [(act_from + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_n)]

        op_summary = {}
        for d in act_dates:
            sdata = fetch_stats_for_date(d)
            for op, opdata in sdata.items():
                if op not in op_summary:
                    op_summary[op] = {"sessions": 0, "diamonds": 0, "cases_ew": 0, "session_times": [], "per_day": {}}
                sess = opdata.get("sessions_completed", 0)
                dia = count_pz6_for_operator(opdata)
                op_summary[op]["sessions"] += sess
                op_summary[op]["diamonds"] += dia
                op_summary[op]["session_times"].extend(opdata.get("session_times", []))
                op_summary[op]["per_day"].setdefault(d, {"sess": 0, "dia": 0, "cew": 0})
                op_summary[op]["per_day"][d]["sess"] = sess
                op_summary[op]["per_day"][d]["dia"] = dia

            ewdata = fetch_ew_stats_for_date(d)
            for op, opdata in ewdata.items():
                if op not in op_summary:
                    op_summary[op] = {"sessions": 0, "diamonds": 0, "cases_ew": 0, "session_times": [], "per_day": {}}
                cew = opdata.get("cases_completed", 0)
                op_summary[op]["cases_ew"] += cew
                op_summary[op]["per_day"].setdefault(d, {"sess": 0, "dia": 0, "cew": 0})
                op_summary[op]["per_day"][d]["cew"] = cew

        active_ops = {op: data for op, data in op_summary.items()
                      if data["sessions"] > 0 or data["diamonds"] > 0 or data["cases_ew"] > 0}

        if not active_ops:
            st.info(f"Brak ruchu w okresie {act_from} – {act_to} (źródło: {prefixes_label}).")
        else:
            total_sessions = sum(d["sessions"] for d in active_ops.values())
            total_diamonds = sum(d["diamonds"] for d in active_ops.values())
            total_cases_ew = sum(d["cases_ew"] for d in active_ops.values())

            col_a1, col_a2, col_a3, col_a4 = st.columns(4)
            col_a1.metric("👥 Aktywni operatorzy", len(active_ops))
            col_a2.metric("💎 Diamenty (suma)", total_diamonds)
            col_a3.metric("📋 Sesje (suma)", total_sessions)
            col_a4.metric("🏢 Casy EW zakończone (suma)", total_cases_ew)

            st.subheader("📊 Per operator w okresie")
            rows = []
            for op, data in active_ops.items():
                conv = round(data["diamonds"] / data["sessions"] * 100, 1) if data["sessions"] > 0 else 0
                rows.append({
                    "Operator": op,
                    "💎 Diamenty": data["diamonds"],
                    "📋 Sesje": data["sessions"],
                    "🏢 Casy EW (zakończone)": data["cases_ew"],
                    "💎/📋 Konwersja %": conv,
                })
            df_act = pd.DataFrame(rows).sort_values(by="💎 Diamenty", ascending=False)
            st.dataframe(df_act, use_container_width=True, hide_index=True)
            st.caption("💎/📋 Konwersja = ile % sesji skończyło się diamentem (PZ6).")

            if days_n <= 14:
                st.markdown("---")
                st.subheader("📊 Wykres słupkowy — diamenty vs sesje vs casy EW")
                chart_data = df_act.set_index("Operator")[["💎 Diamenty", "📋 Sesje", "🏢 Casy EW (zakończone)"]]
                st.bar_chart(chart_data)

                st.subheader("📈 Aktywność dzień po dniu (suma)")
                daily_agg = {d: {"sess": 0, "dia": 0, "cew": 0} for d in act_dates}
                for op, data in active_ops.items():
                    for d, vals in data["per_day"].items():
                        if d in daily_agg:
                            daily_agg[d]["sess"] += vals.get("sess", 0)
                            daily_agg[d]["dia"] += vals.get("dia", 0)
                            daily_agg[d]["cew"] += vals.get("cew", 0)
                df_daily = pd.DataFrame({
                    "Data": list(daily_agg.keys()),
                    "💎 Diamenty": [v["dia"] for v in daily_agg.values()],
                    "📋 Sesje": [v["sess"] for v in daily_agg.values()],
                    "🏢 Casy EW": [v["cew"] for v in daily_agg.values()],
                }).sort_values("Data")
                st.line_chart(df_daily.set_index("Data"))

                st.markdown("---")
                st.subheader("🕐 Aktywność godzinowa (rozkład sesji w dobie)")
                st.caption("Liczone z `session_times` zapisywanych przy każdym zamknięciu sesji.")

                global_hourly = {h: 0 for h in range(24)}
                per_op_hourly = {}
                for op, data in active_ops.items():
                    per_op_hourly[op] = {h: 0 for h in range(24)}
                    for ts in data["session_times"]:
                        try:
                            hour = int(str(ts).split(":")[0])
                            if 0 <= hour <= 23:
                                global_hourly[hour] += 1
                                per_op_hourly[op][hour] += 1
                        except (ValueError, AttributeError, IndexError):
                            pass

                if sum(global_hourly.values()) > 0:
                    df_hourly = pd.DataFrame({
                        "Godzina": [f"{h:02d}:00" for h in range(24)],
                        "Sesje": [global_hourly[h] for h in range(24)],
                    })
                    st.bar_chart(df_hourly.set_index("Godzina"))

                    with st.expander("🔥 Aktywność godzinowa per operator (tabela)"):
                        heatmap_rows = []
                        for op in sorted(per_op_hourly.keys()):
                            if sum(per_op_hourly[op].values()) == 0:
                                continue
                            row = {"Operator": op}
                            for h in range(24):
                                row[f"{h:02d}h"] = per_op_hourly[op][h]
                            heatmap_rows.append(row)
                        if heatmap_rows:
                            df_heat = pd.DataFrame(heatmap_rows)
                            st.dataframe(df_heat, use_container_width=True, hide_index=True)
                            st.caption("Liczby = ile sesji operator zamknął w danej godzinie.")
                else:
                    st.info("Brak `session_times` w wybranym okresie (możliwe że pole nie było jeszcze logowane wtedy).")

                st.markdown("---")
                st.subheader("📅 Operator × dzień")
                detail_rows = []
                for op, data in active_ops.items():
                    row = {"Operator": op}
                    for d in sorted(act_dates):
                        vals = data["per_day"].get(d, {})
                        row[d] = f"{vals.get('dia', 0)}💎 / {vals.get('sess', 0)}📋 / {vals.get('cew', 0)}🏢"
                    detail_rows.append(row)
                df_detail = pd.DataFrame(detail_rows)
                st.dataframe(df_detail, use_container_width=True, hide_index=True)
                st.caption("Format: diamenty 💎 / sesje 📋 / casy EW 🏢")
            else:
                st.info(f"Zakres {days_n} dni > 14 — pokazana tylko tabela sumaryczna. Zwęź zakres, żeby zobaczyć wykresy.")
