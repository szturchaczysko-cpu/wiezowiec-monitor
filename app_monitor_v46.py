import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import json
import re
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
    """{op: {sessions_completed, session_times, "pz_transitions.XXX_to_YYY": N, ...}}
    
    UWAGA: Firestore zapisuje 'pz_transitions.PZx_to_PZy' jako PŁASKIE pole 
    (kropka jest częścią nazwy klucza), bo log_stats używa set(merge=True), nie update().
    """
    result = {}
    for path in col_paths("stats"):
        try:
            docs = db.collection(path).document(date_str).collection("operators").get()
        except Exception:
            continue
        for d in docs:
            data = d.to_dict() or {}
            if d.id not in result:
                result[d.id] = {}
            existing = result[d.id]
            for k, v in data.items():
                if isinstance(v, (int, float)):
                    existing[k] = existing.get(k, 0) + v
                elif isinstance(v, list):
                    existing[k] = existing.get(k, []) + v
                else:
                    existing[k] = v
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
    """Sumuje PZ6 dzienne. Klucze są PŁASKIE: 'pz_transitions.PZx_to_PZ6'.

    WYKLUCZENIA (diagnostyka 2026-05-18, finding agenta):
    - pz_transitions.PZ6_to_PZ6 — re-confirm tego samego case'a (np. operator
      klika "PZ6" wielokrotnie na case'ie który już jest w PZ6, autopilot
      przerabia ten sam case dwa razy, lub UI loguje przeładowania jako transition).
      To NIE jest nowy diament. Marlena 18.05.2026 miała 27 takich re-confirmów.

    Skutek fixa: 18.05.2026 było 118 → po fixie ~69 (rzeczywista wartość ~60).
    """
    SKIP_KEYS = {"pz_transitions.PZ6_to_PZ6"}
    return sum(v for k, v in stats_data.items()
               if isinstance(k, str)
               and k.startswith("pz_transitions.")
               and k.endswith("_to_PZ6")
               and k not in SKIP_KEYS
               and isinstance(v, (int, float)))

# ==========================================
# 📦 PARSOWANIE TYPU TOWARU Z ZAKOŃCZONYCH CASE'ÓW
# ==========================================
# Diamenty (PZ6) NIE są oznaczane typem towaru w stats/global_stats.
# Typ siedzi w `result_tag` zakończonych case'ów jako literał TOWAR_TYP=*
# (fallback: autopilot_messages[-1].content, potem pelna_linia_szturchacza).
_RE_TOWAR_TYP = re.compile(r'TOWAR_TYP\s*[=:]\s*([A-Za-zżźćńółęąś_]+)', re.IGNORECASE)
_RE_KURIER = re.compile(r'KURIER_PRZEWOZNIK\s*[=:]\s*([A-Za-z_]+)', re.IGNORECASE)

def _search_in_case(case_data, pattern):
    """Szuka wzorca w polach: result_tag → autopilot_messages[-1] → pelna_linia_szturchacza."""
    for field in ("result_tag", "pelna_linia_szturchacza"):
        val = case_data.get(field) or ""
        if isinstance(val, str):
            m = pattern.search(val)
            if m:
                return m.group(1)
    messages = case_data.get("autopilot_messages") or []
    if messages and isinstance(messages, list):
        last = messages[-1] if isinstance(messages[-1], dict) else {}
        content = last.get("content", "") if isinstance(last, dict) else ""
        if isinstance(content, str):
            m = pattern.search(content)
            if m:
                return m.group(1)
    return None

def parse_towar_typ(case_data):
    raw = _search_in_case(case_data, _RE_TOWAR_TYP)
    return raw.lower() if raw else None

def parse_kurier(case_data):
    raw = _search_in_case(case_data, _RE_KURIER)
    return raw.lower() if raw else None

def normalize_typ(raw):
    """Spłaszcza warianty pisowni do kategorii biznesowych."""
    if not raw:
        return "Nieznany"
    r = str(raw).strip().lower()
    if "kolektor" in r:
        return "Kolektor"
    if "skrzyn" in r:
        return "Skrzynia biegów"
    return raw.capitalize() if isinstance(raw, str) else "Nieznany"

def normalize_kurier(raw):
    if not raw:
        return "?"
    r = str(raw).strip().upper()
    if r in ("UPS",):
        return "UPS"
    if r in ("FEDEX",):
        return "FedEx"
    if r in ("DBSCHENKER", "DB_SCHENKER", "SCHENKER"):
        return "DB Schenker"
    return r.capitalize()

# ==========================================
# 🗂️ CACHE TYPÓW TOWARU (z agenta)
# ==========================================
# Kolekcja typ_towaru_cache/{numer_zamowienia} = {
#   numer_zamowienia, resolved_index, tartID, tartNazwa,
#   kategoria: 'Kolektor' | 'Skrzynia biegów' | 'Inne' | 'Nieznany',
#   source: 'sql' | 'index_handlowy' | 'lookup_failed',
#   updated_at: timestamp
# }
@st.cache_data(ttl=60)
def fetch_typ_cache():
    """Pobiera całą kolekcję typ_towaru_cache i zwraca {numer_zamowienia: dict, ...} + max(updated_at)."""
    cache = {}
    max_updated = None
    try:
        docs = list(db.collection("typ_towaru_cache").get())
    except Exception:
        return cache, None, 0
    for d in docs:
        data = d.to_dict() or {}
        cache[str(d.id)] = data
        up = data.get("updated_at")
        if up:
            try:
                if max_updated is None or up > max_updated:
                    max_updated = up
            except Exception:
                pass
    return cache, max_updated, len(docs)

def resolve_typ_for_case(case_data, cache_dict):
    """Lookup typu towaru: cache (po numer_zamowienia) → regex → 'Brak danych'.
    Zwraca (kategoria_biznesowa, źródło, szczegóły_tartNazwa)."""
    nr = str(case_data.get("numer_zamowienia") or "")
    if nr and nr in cache_dict:
        c = cache_dict[nr]
        kat = c.get("kategoria") or "Nieznany"
        tart = c.get("tartNazwa") or ""
        return kat, "cache", tart
    # Fallback regex
    raw = parse_towar_typ(case_data)
    if raw:
        return normalize_typ(raw), "regex", raw
    return "Brak danych", "brak", ""

@st.cache_data(ttl=180)
def fetch_pz6_cases_with_metadata(_prefixes_tuple, _cache_max_updated_iso):
    """Skanuje wszystkie case'y z ew_cases_archived + ew_cases.
    Typ pobiera z typ_towaru_cache (priorytet) → regex (fallback) → 'Brak danych'.
    _prefixes_tuple i _cache_max_updated_iso w sygnaturze służą do invalidacji cache."""
    prefixes = list(_prefixes_tuple)
    typ_cache, _, _ = fetch_typ_cache()
    results = []
    seen = set()
    for prefix in prefixes:
        for col_name in (f"{prefix}ew_cases_archived", f"{prefix}ew_cases"):
            try:
                docs = list(db.collection(col_name).get())
            except Exception:
                continue
            for d in docs:
                key = (col_name, d.id)
                if key in seen:
                    continue
                seen.add(key)
                data = d.to_dict() or {}
                completed_at = data.get("completed_at")
                completed_date = None
                if completed_at:
                    try:
                        completed_date = completed_at.strftime("%Y-%m-%d") if hasattr(completed_at, "strftime") else str(completed_at)[:10]
                    except Exception:
                        completed_date = None
                kat, src, tart_detail = resolve_typ_for_case(data, typ_cache)
                results.append({
                    "case_id": d.id,
                    "operator": data.get("assigned_to") or data.get("autopilot_assigned_to") or "?",
                    "completed_date": completed_date,
                    "completed_at": completed_at,
                    "status": data.get("status", "?"),
                    "result_pz": data.get("result_pz"),
                    "numer_zamowienia": data.get("numer_zamowienia", "?"),
                    "index_handlowy": data.get("index_handlowy", ""),
                    "grupa": data.get("grupa", "?"),
                    "towar_typ": kat,
                    "towar_typ_source": src,
                    "towar_typ_detail": tart_detail,
                    "kurier": normalize_kurier(parse_kurier(data)),
                    "source": col_name,
                    "_raw_data": data,
                })
    return results

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
with st.expander("🔍 Diagnostyka źródeł danych (otwórz jak coś nie gra)", expanded=False):
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
                # Klucze są PŁASKIE: 'pz_transitions.PZx_to_PZ6'
                total_pz6_today += sum(v for k, v in data.items()
                                       if isinstance(k, str)
                                       and k.startswith("pz_transitions.")
                                       and k.endswith("_to_PZ6")
                                       and isinstance(v, (int, float)))
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
tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Batche + diamenty (live)",
    "📈 Aktywność operatorów (zakres dat)",
    "💎 Drill-down per operator",
    "📊 Prosty widok",
])

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
                    op_summary[op] = {"sessions": 0, "diamonds_period": 0, "cases_ew": 0, "session_times": [], "per_day": {}}
                sess = opdata.get("sessions_completed", 0)
                dia = count_pz6_for_operator(opdata)
                op_summary[op]["sessions"] += sess
                op_summary[op]["diamonds_period"] += dia
                op_summary[op]["session_times"].extend(opdata.get("session_times", []))
                op_summary[op]["per_day"].setdefault(d, {"sess": 0, "dia": 0, "cew": 0})
                op_summary[op]["per_day"][d]["sess"] = sess
                op_summary[op]["per_day"][d]["dia"] = dia

            ewdata = fetch_ew_stats_for_date(d)
            for op, opdata in ewdata.items():
                if op not in op_summary:
                    op_summary[op] = {"sessions": 0, "diamonds_period": 0, "cases_ew": 0, "session_times": [], "per_day": {}}
                cew = opdata.get("cases_completed", 0)
                op_summary[op]["cases_ew"] += cew
                op_summary[op]["per_day"].setdefault(d, {"sess": 0, "dia": 0, "cew": 0})
                op_summary[op]["per_day"][d]["cew"] = cew

        # Dorzuć All-time diamenty z global_stats (autorytatywne źródło)
        global_diamonds_t2 = fetch_global_diamonds()
        for op, total in global_diamonds_t2.items():
            if op not in op_summary:
                op_summary[op] = {"sessions": 0, "diamonds_period": 0, "cases_ew": 0, "session_times": [], "per_day": {}}
            op_summary[op]["diamonds_alltime"] = total

        active_ops = {op: data for op, data in op_summary.items()
                      if data["sessions"] > 0 or data.get("diamonds_period", 0) > 0
                      or data["cases_ew"] > 0 or data.get("diamonds_alltime", 0) > 0}

        if not active_ops:
            st.info(f"Brak ruchu w okresie {act_from} – {act_to} (źródło: {prefixes_label}).")
        else:
            total_sessions = sum(d["sessions"] for d in active_ops.values())
            total_diamonds_period = sum(d.get("diamonds_period", 0) for d in active_ops.values())
            total_diamonds_alltime = sum(d.get("diamonds_alltime", 0) for d in active_ops.values())
            total_cases_ew = sum(d["cases_ew"] for d in active_ops.values())

            col_a1, col_a2, col_a3, col_a4, col_a5 = st.columns(5)
            col_a1.metric("👥 Aktywni operatorzy", len(active_ops))
            col_a2.metric("💎 All-time (suma)", total_diamonds_alltime, help="Suma total_diamonds z global_stats dla operatorów z ruchem w okresie. Autorytatywne źródło.")
            col_a3.metric("💎 W okresie (z pz_trans.)", total_diamonds_period, help="Liczone z pz_transitions._to_PZ6 w stats/{date}. Może być 0 jeśli log_stats nie zapisywał transitions w tych dniach.")
            col_a4.metric("📋 Sesje (suma)", total_sessions)
            col_a5.metric("🏢 Casy EW (suma)", total_cases_ew)

            if total_diamonds_alltime > 0 and total_diamonds_period == 0:
                st.caption("ℹ️ Diamenty w okresie = 0, ale operatorzy mają historyczne all-time. Pole `pz_transitions._to_PZ6` w `stats/{date}` jest puste — możliwe że `total_diamonds` było dosypywane innym mechanizmem niż `log_stats`.")

            st.subheader("📊 Per operator w okresie")
            rows = []
            for op, data in active_ops.items():
                conv = round(data.get("diamonds_period", 0) / data["sessions"] * 100, 1) if data["sessions"] > 0 else 0
                rows.append({
                    "Operator": op,
                    "💎 All-time": data.get("diamonds_alltime", 0),
                    "💎 W okresie": data.get("diamonds_period", 0),
                    "📋 Sesje": data["sessions"],
                    "🏢 Casy EW (zakończone)": data["cases_ew"],
                    "💎/📋 Konwersja %": conv,
                })
            df_act = pd.DataFrame(rows).sort_values(by=["💎 All-time", "📋 Sesje"], ascending=[False, False])
            st.dataframe(df_act, use_container_width=True, hide_index=True)
            st.caption("💎 All-time = z `global_stats.total_diamonds` (autorytatywne). 💎 W okresie = z `stats/{date}.pz_transitions._to_PZ6`. Konwersja % = w okresie / sesje.")

            if days_n <= 14:
                st.markdown("---")
                st.subheader("📊 Wykres słupkowy — all-time vs sesje vs casy EW")
                chart_data = df_act.set_index("Operator")[["💎 All-time", "📋 Sesje", "🏢 Casy EW (zakończone)"]]
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

# ============================================================
# ============ ZAKŁADKA 3: drill-down per operator ===========
# ============================================================
with tab3:
    # --- SEKCJA A: ŁĄCZNIE ---
    st.header("🏆 Łącznie — wszyscy operatorzy")

    global_dia_t3 = fetch_global_diamonds()
    total_t3 = sum(global_dia_t3.values())
    n_ops_t3 = sum(1 for v in global_dia_t3.values() if v > 0)

    colT1, colT2, colT3 = st.columns(3)
    colT1.metric("💎 Diamentów łącznie", total_t3)
    colT2.metric("👥 Operatorów z diamentami", n_ops_t3)
    avg_per_op = round(total_t3 / n_ops_t3, 1) if n_ops_t3 > 0 else 0
    colT3.metric("📊 Średnio na operatora", avg_per_op)

    if global_dia_t3:
        rows_all = []
        for op, v in sorted(global_dia_t3.items(), key=lambda x: x[1], reverse=True):
            if v == 0:
                continue
            rows_all.append({
                "Operator": op,
                "Grupa": op_to_grupa.get(op, "?"),
                "💎 All-time": v,
                "% udziału": round(v / total_t3 * 100, 1) if total_t3 > 0 else 0,
            })
        if rows_all:
            df_all = pd.DataFrame(rows_all)
            colA1, colA2 = st.columns([2, 3])
            with colA1:
                st.dataframe(df_all, use_container_width=True, hide_index=True)
            with colA2:
                st.bar_chart(df_all.set_index("Operator")["💎 All-time"])
    else:
        st.info("Brak danych w global_stats.")

    # --- SEKCJA B: DRILL-DOWN ---
    st.markdown("---")
    st.header("🔍 Wybierz operatora — szczegóły")

    ALL_OPS_KEY = "🌍 WSZYSCY OPERATORZY"

    all_known = sorted(set(global_dia_t3.keys()) | set(op_to_grupa.keys()))
    if not all_known:
        st.info("Brak operatorów do wybrania.")
    else:
        all_known_sorted = sorted(
            all_known,
            key=lambda op: (-global_dia_t3.get(op, 0), op)
        )
        options_with_all = [ALL_OPS_KEY] + all_known_sorted
        picked = st.selectbox(
            "Operator:",
            options=options_with_all,
            key="_drilldown_op_v2",
            format_func=lambda op: f"🌍 WSZYSCY  —  💎 {total_t3}" if op == ALL_OPS_KEY else f"{op}  —  💎 {global_dia_t3.get(op, 0)}"
        )

        is_all_ops = (picked == ALL_OPS_KEY)

        if picked:
            # KARTA (operator lub globalna)
            if is_all_ops:
                colB1, colB2, colB3, colB4 = st.columns(4)
                colB1.metric("Operator", "WSZYSCY")
                colB2.metric("👥 Liczba", n_ops_t3)
                colB3.metric("💎 All-time (suma)", total_t3)
                colB4.metric("📊 Średnio/op", avg_per_op)
                picked_alltime = total_t3
            else:
                picked_alltime = global_dia_t3.get(picked, 0)
                picked_grupa = op_to_grupa.get(picked, "?")
                colB1, colB2, colB3, colB4 = st.columns(4)
                colB1.metric("Operator", picked)
                colB2.metric("Grupa", picked_grupa)
                colB3.metric("💎 All-time", picked_alltime)
                rank = sum(1 for v in global_dia_t3.values() if v > picked_alltime) + 1
                colB4.metric("🏆 Miejsce w rankingu", f"#{rank} / {n_ops_t3}")

            # Zakres dat
            st.subheader("📅 Zakres dat dla historii dziennej")
            colD1, colD2 = st.columns(2)
            with colD1:
                dd_from = st.date_input("Od:", value=today.date() - timedelta(days=29), key="_dd_from")
            with colD2:
                dd_to = st.date_input("Do:", value=today.date(), key="_dd_to")

            dd_days_n = (dd_to - dd_from).days + 1
            if dd_days_n < 1:
                st.error("Data 'Od' musi być przed lub równa 'Do'.")
            else:
                dd_dates = [(dd_from + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(dd_days_n)]

                # Per dzień zbieramy dane dla wybranego operatora
                per_day = {}
                period_total_dia = 0
                period_total_sess = 0
                period_total_cew = 0
                transitions_period = {}

                for d in dd_dates:
                    sdata = fetch_stats_for_date(d)
                    ewdata = fetch_ew_stats_for_date(d)

                    if is_all_ops:
                        # Agreguj WSZYSTKICH operatorów w danym dniu
                        sess = 0
                        dia = 0
                        cew = 0
                        day_trans = {}
                        for op_name_iter, op_data in sdata.items():
                            sess_v = op_data.get("sessions_completed", 0)
                            sess += sess_v if isinstance(sess_v, (int, float)) else 0
                            dia += count_pz6_for_operator(op_data)
                            for k, v in op_data.items():
                                if isinstance(k, str) and k.startswith("pz_transitions.") and k.endswith("_to_PZ6") and isinstance(v, (int, float)):
                                    short = k.replace("pz_transitions.", "")
                                    day_trans[short] = day_trans.get(short, 0) + v
                                    transitions_period[short] = transitions_period.get(short, 0) + v
                        for op_name_iter, ew_op in ewdata.items():
                            cew_v = ew_op.get("cases_completed", 0)
                            cew += cew_v if isinstance(cew_v, (int, float)) else 0
                    else:
                        # Tylko wybrany operator
                        op_data = sdata.get(picked, {})
                        sess_v = op_data.get("sessions_completed", 0)
                        sess = sess_v if isinstance(sess_v, (int, float)) else 0
                        dia = count_pz6_for_operator(op_data)
                        day_trans = {}
                        for k, v in op_data.items():
                            if isinstance(k, str) and k.startswith("pz_transitions.") and k.endswith("_to_PZ6") and isinstance(v, (int, float)):
                                short = k.replace("pz_transitions.", "")
                                day_trans[short] = v
                                transitions_period[short] = transitions_period.get(short, 0) + v
                        ew_op = ewdata.get(picked, {})
                        cew = ew_op.get("cases_completed", 0)

                    per_day[d] = {"sessions": sess, "diamonds": dia, "transitions": day_trans, "cases_ew": cew}
                    period_total_dia += dia
                    period_total_sess += sess
                    period_total_cew += cew

                # METRYKI OKRESU
                who_label = "WSZYSCY OPERATORZY" if is_all_ops else picked
                st.markdown(f"#### Okres: {dd_from} – {dd_to} ({dd_days_n} dni) — {who_label}")
                colP1, colP2, colP3, colP4 = st.columns(4)
                colP1.metric("💎 Diamenty (okres)", period_total_dia)
                colP2.metric("📋 Sesje (okres)", period_total_sess)
                colP3.metric("🏢 Casy EW (okres)", period_total_cew)
                conv_pct = round(period_total_dia / period_total_sess * 100, 1) if period_total_sess > 0 else 0
                colP4.metric("💎/📋 Konwersja %", f"{conv_pct}%")

                # WYKRES DZIENNY
                if period_total_dia > 0 or period_total_sess > 0:
                    st.subheader("📈 Dzienne: diamenty + sesje + casy EW")
                    df_day = pd.DataFrame({
                        "Data": dd_dates,
                        "💎 Diamenty": [per_day[d]["diamonds"] for d in dd_dates],
                        "📋 Sesje": [per_day[d]["sessions"] for d in dd_dates],
                        "🏢 Casy EW": [per_day[d]["cases_ew"] for d in dd_dates],
                    })
                    st.line_chart(df_day.set_index("Data"))

                # TABELA DZIENNA
                st.subheader("📋 Dzień po dniu")
                table_rows = []
                for d in dd_dates:
                    pd_data = per_day[d]
                    trans_str = ", ".join([f"{k}={v}" for k, v in sorted(pd_data["transitions"].items(), key=lambda x: -x[1])]) or "—"
                    table_rows.append({
                        "Data": d,
                        "💎 Diamenty": pd_data["diamonds"],
                        "📋 Sesje": pd_data["sessions"],
                        "🏢 Casy EW": pd_data["cases_ew"],
                        "Transitions → PZ6": trans_str,
                    })
                # Filtr: pokaż tylko dni z jakimkolwiek ruchem
                table_rows_with_action = [r for r in table_rows if r["💎 Diamenty"] > 0 or r["📋 Sesje"] > 0 or r["🏢 Casy EW"] > 0]
                if table_rows_with_action:
                    df_table = pd.DataFrame(table_rows_with_action)
                    st.dataframe(df_table, use_container_width=True, hide_index=True)
                    show_empty = st.checkbox("Pokaż też dni bez ruchu", value=False, key="_dd_show_empty")
                    if show_empty:
                        st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
                else:
                    st.info(f"Operator {picked} nie miał ruchu w tym okresie.")

                # BREAKDOWN TRANSITIONS
                if transitions_period:
                    st.subheader("🔀 Rozkład przejść do PZ6 (w okresie)")
                    tr_rows = [{"Transition": k, "Liczba": v, "% diamentów": round(v / period_total_dia * 100, 1) if period_total_dia > 0 else 0}
                               for k, v in sorted(transitions_period.items(), key=lambda x: -x[1])]
                    df_tr = pd.DataFrame(tr_rows)
                    colTR1, colTR2 = st.columns([2, 3])
                    with colTR1:
                        st.dataframe(df_tr, use_container_width=True, hide_index=True)
                    with colTR2:
                        st.bar_chart(df_tr.set_index("Transition")["Liczba"])
                    st.caption("Każda transition = jeden zarejestrowany skok stanu PZ kończący się na PZ6. PZ6→PZ6 to potwierdzenie/odświeżenie stanu (też liczone do total_diamonds w apce).")

                # ==========================================
                # 📦 DIAMENTY PER TYP TOWARU
                # ==========================================
                st.markdown("---")
                st.subheader("📦 Diamenty per typ towaru")

                # --- Status cache typów ---
                typ_cache_dict, cache_max_updated, cache_n = fetch_typ_cache()
                cache_max_iso = cache_max_updated.isoformat() if cache_max_updated and hasattr(cache_max_updated, "isoformat") else str(cache_max_updated or "")

                cutoff_label = cache_max_updated.strftime("%Y-%m-%d %H:%M") if cache_max_updated and hasattr(cache_max_updated, "strftime") else "brak (cache pusty)"

                colCache1, colCache2, colCache3, colCache4 = st.columns([2, 2, 2, 1])
                colCache1.metric("🗂️ Wpisów w cache", cache_n)
                colCache2.metric("📅 Cache świeży do", cutoff_label)
                if colCache3.button("🔄 Pobierz świeży cache z Firestore", help="Czyści lokalny cache streamlita i ponownie pobiera typ_towaru_cache"):
                    st.cache_data.clear()
                    st.rerun()

                # --- Fetch case'ów ---
                pz6_all = fetch_pz6_cases_with_metadata(tuple(active_prefixes), cache_max_iso)

                # Filtr per operator (jeśli nie WSZYSCY)
                if is_all_ops:
                    pz6_filtered = pz6_all
                else:
                    pz6_filtered = [c for c in pz6_all if c["operator"] == picked]

                # --- Statystyki źródła typu ---
                n_total_scanned = len(pz6_filtered)
                n_from_cache = sum(1 for c in pz6_filtered if c["towar_typ_source"] == "cache")
                n_from_regex = sum(1 for c in pz6_filtered if c["towar_typ_source"] == "regex")
                n_no_data = sum(1 for c in pz6_filtered if c["towar_typ_source"] == "brak")

                colSrc1, colSrc2, colSrc3, colSrc4 = st.columns(4)
                colSrc1.metric("📥 Skanowanych", n_total_scanned)
                colSrc2.metric("✅ Z cache (SQL)", n_from_cache, f"{round(n_from_cache/n_total_scanned*100, 1)}%" if n_total_scanned else "0%")
                colSrc3.metric("🔣 Z regexa (fallback)", n_from_regex, f"{round(n_from_regex/n_total_scanned*100, 1)}%" if n_total_scanned else "0%")
                colSrc4.metric("❓ Brak danych", n_no_data, f"{round(n_no_data/n_total_scanned*100, 1)}%" if n_total_scanned else "0%")

                # --- PANEL: brakujące numery do Dashboard DSG ---
                with st.expander("📦 Brakujące typy → Dashboard DSG (skopiuj listę → wklej w app kolegi)", expanded=(n_no_data > 0 and cache_n == 0)):
                    st.markdown(
                        "**Jak to działa:** monitor nie ma dostępu do baz SQL. Cache buduje funkcja "
                        "`klasyfikuj_zamowienia` w Dashboard DSG (~5 sek deterministyczny SQL, bez agenta LLM). "
                        "Workflow: skopiuj listę numerów poniżej → wklej w Dashboard DSG → klik **Klasyfikuj** → wróć tutaj → "
                        "klik **🔄 Pobierz świeży cache** (przycisk u góry sidebara)."
                    )

                    def _is_valid_nr(s):
                        s = str(s or "").strip()
                        return s.isdigit() and 5 <= len(s) <= 7
                    missing_nrs = sorted(
                        {c["numer_zamowienia"] for c in pz6_filtered
                         if c["towar_typ_source"] == "brak"
                         and _is_valid_nr(c["numer_zamowienia"])},
                        key=lambda x: int(x)
                    )
                    skipped_garbage = sum(
                        1 for c in pz6_filtered
                        if c["towar_typ_source"] == "brak"
                        and not _is_valid_nr(c["numer_zamowienia"])
                    )

                    if not missing_nrs:
                        st.success(f"✅ Brak brakujących numerów. Cache pełny ({cache_n} wpisów).")
                    else:
                        col_a, col_b = st.columns(2)
                        col_a.metric("❓ Brakuje typu dla", f"{len(missing_nrs)} numerów")
                        col_b.metric("📊 Cache obecnie", f"{cache_n} wpisów")
                        if skipped_garbage:
                            st.caption(f"⚠️ Odfiltrowano {skipped_garbage} case'ów z niepoprawnym numerem zamówienia (puste / 'i, wyżej' / 'ZW...' / inne śmieci z CSV).")

                        numbers_text = "\n".join(missing_nrs)
                        st.code(numbers_text, language="text")
                        st.caption(
                            f"📋 Skopiuj **{len(missing_nrs)} numerów** powyżej (ikonka 'copy' w prawym górnym rogu bloku) "
                            f"i wklej w Dashboard DSG → przycisk **Klasyfikuj zamówienia**. "
                            f"Funkcja zapisze wyniki do `typ_towaru_cache` w Firestore."
                        )

                # --- Alert gdy cache pusty ---
                if cache_n == 0:
                    st.warning(
                        "⚠️ Kolekcja `typ_towaru_cache` jest pusta. "
                        "Otwórz powyższy panel, skopiuj listę numerów i wklej w Dashboard DSG → 'Klasyfikuj zamówienia'."
                    )


                if not pz6_filtered:
                    st.info(
                        f"Brak zarchiwizowanych case'ów dla "
                        f"{'wszystkich operatorów' if is_all_ops else picked}. "
                        f"Przełącz źródło w sidebarze, jeśli szukasz historii."
                    )
                else:
                    # Filtr po dacie (zakres dd_from .. dd_to) jeśli case ma completed_date
                    pz6_in_range = []
                    pz6_no_date = []
                    for c in pz6_filtered:
                        cd = c.get("completed_date")
                        if cd and dd_from.strftime("%Y-%m-%d") <= cd <= dd_to.strftime("%Y-%m-%d"):
                            pz6_in_range.append(c)
                        elif not cd:
                            pz6_no_date.append(c)

                    # Podsumowanie globalne typu
                    typ_counts_all = {}
                    for c in pz6_filtered:
                        typ_counts_all[c["towar_typ"]] = typ_counts_all.get(c["towar_typ"], 0) + 1
                    total_pz6 = sum(typ_counts_all.values())

                    # Metryki typu
                    colT_kol, colT_skr, colT_unk, colT_total = st.columns(4)
                    n_kol = typ_counts_all.get("Kolektor", 0)
                    n_skr = typ_counts_all.get("Skrzynia biegów", 0)
                    n_inne = typ_counts_all.get("Inne", 0)
                    n_brak = typ_counts_all.get("Brak danych", 0) + typ_counts_all.get("Nieznany", 0)
                    colT_total.metric("📦 Razem", total_pz6)
                    colT_kol.metric(
                        "🔧 Kolektor",
                        n_kol,
                        f"{round(n_kol / total_pz6 * 100, 1)}%" if total_pz6 > 0 else "0%"
                    )
                    colT_skr.metric(
                        "⚙️ Skrzynia biegów",
                        n_skr,
                        f"{round(n_skr / total_pz6 * 100, 1)}%" if total_pz6 > 0 else "0%"
                    )
                    colT_unk.metric(
                        "❓ Brak danych",
                        n_brak,
                        f"{round(n_brak / total_pz6 * 100, 1)}%" if total_pz6 > 0 else "0%"
                    )

                    # --- Lista case'ów po cutoff cache (jawnie oznaczone) ---
                    if cache_max_updated:
                        cutoff_date_str = cache_max_updated.strftime("%Y-%m-%d") if hasattr(cache_max_updated, "strftime") else str(cache_max_updated)[:10]
                        after_cutoff = [c for c in pz6_filtered
                                        if c["towar_typ_source"] == "brak"
                                        and c.get("completed_date")
                                        and c["completed_date"] > cutoff_date_str]
                        if after_cutoff:
                            st.info(
                                f"📍 **Cutoff cache: {cutoff_date_str}.** "
                                f"{len(after_cutoff)} case'ów zakończonych PO tej dacie nie ma jeszcze typu (cache ich nie widział). "
                                f"Odśwież cache → wybierz 'Tylko brakujące' w expanderze powyżej."
                            )

                    # Alert gdy wszystko Brak danych
                    if n_kol == 0 and n_skr == 0 and n_brak > 0 and cache_n == 0:
                        st.warning(
                            f"⚠️ Wszystkie {n_brak} case'y są bez typu — cache pusty i regex nie znajduje literałów. "
                            "Zbuduj cache przez agenta (expander powyżej)."
                        )

                    # DIAGNOSTYKA REGEX (tylko jak są brakujące)
                    if n_brak > 0:
                        with st.expander(f"🔬 Diagnostyka — surowe pola {min(3, n_brak)} case'ów bez typu"):
                            unk_cases = [c for c in pz6_filtered if c["towar_typ_source"] == "brak"]
                            st.caption(f"Bez typu: {len(unk_cases)}. Pokazuję pierwsze 3.")
                            for i, c in enumerate(unk_cases[:3]):
                                st.markdown(f"#### Case #{i+1}: `{c['case_id']}` | nr_zam: `{c['numer_zamowienia']}`")
                                raw = c.get("_raw_data", {})
                                st.markdown(f"**status**: `{raw.get('status', '?')}` | **result_pz**: `{raw.get('result_pz', '?')}` | **completed_at**: `{raw.get('completed_at', '?')}` | **index_handlowy**: `{raw.get('index_handlowy', '')}`")
                                for fname in ("result_tag", "pelna_linia_szturchacza"):
                                    val = raw.get(fname, "")
                                    if val:
                                        has_towar = "towar_typ" in str(val).lower()
                                        flag = "✅ JEST 'towar_typ'" if has_towar else "❌ brak 'towar_typ'"
                                        st.markdown(f"**`{fname}`** ({flag}):")
                                        st.code(str(val)[:1500], language="text")
                                    else:
                                        st.markdown(f"**`{fname}`**: _puste_")
                                st.markdown("---")

                    # Wykres słupkowy typów
                    df_typ = pd.DataFrame([
                        {"Typ": k, "Liczba": v}
                        for k, v in sorted(typ_counts_all.items(), key=lambda x: -x[1])
                    ])
                    if not df_typ.empty:
                        st.bar_chart(df_typ.set_index("Typ"))

                    # Tabela per kurier x typ
                    if pz6_filtered:
                        st.markdown("**📊 Rozbicie kurier × typ towaru:**")
                        kurier_typ_grid = {}
                        for c in pz6_filtered:
                            kk = c.get("kurier", "?")
                            tt = c.get("towar_typ", "Nieznany")
                            kurier_typ_grid.setdefault(kk, {}).setdefault(tt, 0)
                            kurier_typ_grid[kk][tt] += 1
                        all_typy = sorted({tt for kt in kurier_typ_grid.values() for tt in kt.keys()})
                        rows_kt = []
                        for kk in sorted(kurier_typ_grid.keys()):
                            row = {"Kurier": kk}
                            for tt in all_typy:
                                row[tt] = kurier_typ_grid[kk].get(tt, 0)
                            row["Σ"] = sum(kurier_typ_grid[kk].values())
                            rows_kt.append(row)
                        st.dataframe(pd.DataFrame(rows_kt), use_container_width=True, hide_index=True)

                    if is_all_ops:
                        st.markdown("**👥 Rozbicie operator × typ towaru:**")
                        op_typ_grid = {}
                        for c in pz6_filtered:
                            opn = c.get("operator", "?")
                            tt = c.get("towar_typ", "Nieznany")
                            op_typ_grid.setdefault(opn, {}).setdefault(tt, 0)
                            op_typ_grid[opn][tt] += 1
                        all_typy2 = sorted({tt for ot in op_typ_grid.values() for tt in ot.keys()})
                        rows_ot = []
                        for opn in sorted(op_typ_grid.keys(), key=lambda x: -sum(op_typ_grid[x].values())):
                            row = {"Operator": opn}
                            for tt in all_typy2:
                                row[tt] = op_typ_grid[opn].get(tt, 0)
                            row["Σ"] = sum(op_typ_grid[opn].values())
                            rows_ot.append(row)
                        st.dataframe(pd.DataFrame(rows_ot), use_container_width=True, hide_index=True)

                    if pz6_in_range:
                        st.markdown(f"**📈 W okresie {dd_from} – {dd_to}: {len(pz6_in_range)} case'ów z `completed_at`**")
                        daily_typ = {d: {"Kolektor": 0, "Skrzynia biegów": 0, "Nieznany": 0} for d in dd_dates}
                        for c in pz6_in_range:
                            d_str = c["completed_date"]
                            if d_str in daily_typ:
                                tt = c.get("towar_typ", "Nieznany")
                                daily_typ[d_str].setdefault(tt, 0)
                                daily_typ[d_str][tt] += 1
                        df_daily_typ = pd.DataFrame([
                            {"Data": d, **typs}
                            for d, typs in daily_typ.items()
                        ]).sort_values("Data")
                        st.area_chart(df_daily_typ.set_index("Data"))

                    if pz6_no_date:
                        st.caption(f"ℹ️ {len(pz6_no_date)} case'ów bez `completed_at` — nie wpadły do dziennego wykresu.")

                    with st.expander(f"📋 Pełna lista zarchiwizowanych case'ów ({len(pz6_filtered)})"):
                        SRC_LABEL = {"cache": "✅ cache", "regex": "🔣 regex", "brak": "❓ brak"}
                        detail_rows = []
                        for c in pz6_filtered:
                            detail_rows.append({
                                "Data zak.": c.get("completed_date") or "?",
                                "Operator": c.get("operator", "?"),
                                "Grupa": c.get("grupa", "?"),
                                "Status": c.get("status", "?"),
                                "Result PZ": c.get("result_pz") or "?",
                                "Nr zam.": c.get("numer_zamowienia", "?"),
                                "Typ": c.get("towar_typ", "Brak danych"),
                                "Źródło typu": SRC_LABEL.get(c.get("towar_typ_source", "brak"), "?"),
                                "Szczegół (tartNazwa)": c.get("towar_typ_detail", ""),
                                "Kurier": c.get("kurier", "?"),
                                "Case ID": c.get("case_id", "?"),
                            })
                        df_detail2 = pd.DataFrame(detail_rows).sort_values(by="Data zak.", ascending=False)
                        st.dataframe(df_detail2, use_container_width=True, hide_index=True)



# ============================================================
# ============== ZAKŁADKA 4: PROSTY WIDOK =====================
# ============================================================
with tab4:
    st.header("📊 Prosty widok — diamenty (PZ6) per dzień")

    # === Filtry ===
    fcol1, fcol2, fcol3 = st.columns([2, 2, 2])

    with fcol1:
        t4_from = st.date_input("📅 Od:", value=today.date() - timedelta(days=29), key="_t4_from")
    with fcol2:
        t4_to = st.date_input("📅 Do:", value=today.date(), key="_t4_to")
    with fcol3:
        # Operator z global_diamonds (tak jak tab3 ich wybiera)
        global_dia_t4 = fetch_global_diamonds()
        all_known_t4 = sorted(set(global_dia_t4.keys()) | set(op_to_grupa.keys()))
        all_known_sorted_t4 = sorted(all_known_t4, key=lambda op: (-global_dia_t4.get(op, 0), op))
        ALL_KEY_T4 = "🌍 WSZYSCY OPERATORZY"
        picked_t4 = st.selectbox(
            "👤 Operator:",
            options=[ALL_KEY_T4] + all_known_sorted_t4,
            key="_t4_op",
            format_func=lambda op: f"🌍 WSZYSCY" if op == ALL_KEY_T4 else f"{op} (💎 {global_dia_t4.get(op, 0)})"
        )

    is_all_t4 = (picked_t4 == ALL_KEY_T4)

    # Filtr typu — operuje tylko gdy user wybrał konkretny typ (filtruje case'y PZ6 po typie)
    typ_t4 = st.radio(
        "🏷️ Typ towaru:",
        options=["Wszystkie", "🔩 Kolektor", "🔧 Skrzynia biegów", "❓ Nieprzypisane"],
        index=0,
        horizontal=True,
        key="_t4_typ",
    )

    # === Walidacja ===
    if t4_to < t4_from:
        st.error("Data 'Do' musi być po 'Od'.")
        st.stop()

    days_n_t4 = (t4_to - t4_from).days + 1
    dates_t4 = [(t4_from + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_n_t4)]

    # === Wybór źródła danych w zależności od filtra typu ===
    # typ=Wszystkie → stats/{data} (jak tab3 — sumuje pz_transitions.PZx_to_PZ6, dużo danych)
    # typ!=Wszystkie → ew_cases_archived (case'y PZ6 z typem towaru)
    use_archived = (typ_t4 != "Wszystkie")

    TYP_MAP = {
        "🔩 Kolektor": "Kolektor",
        "🔧 Skrzynia biegów": "Skrzynia biegów",
        "❓ Nieprzypisane": "Nieprzypisane",  # = wszystko poza Kolektor/Skrzynia
    }

    per_day_t4 = {d: 0 for d in dates_t4}
    period_total_t4 = 0

    if not use_archived:
        # === Tryb stats (jak tab3) ===
        for d in dates_t4:
            sdata = fetch_stats_for_date(d)
            if is_all_t4:
                day_total = 0
                for op_name, op_data in sdata.items():
                    day_total += count_pz6_for_operator(op_data)
            else:
                op_data = sdata.get(picked_t4, {})
                day_total = count_pz6_for_operator(op_data)
            per_day_t4[d] = day_total
            period_total_t4 += day_total
    else:
        # === Tryb archived (case'y PZ6 z typem) ===
        typ_cache_dict_t4, cache_max_t4, _ = fetch_typ_cache()
        cache_iso_t4 = cache_max_t4.isoformat() if cache_max_t4 and hasattr(cache_max_t4, "isoformat") else "none"
        all_cases_t4 = fetch_pz6_cases_with_metadata(tuple(active_prefixes), cache_iso_t4)

        def _is_pz6_t4(c):
            rp = c.get("result_pz")
            if rp is None:
                return False
            return str(rp).strip().upper() in ("6", "PZ6", "PZ_6")

        def _kat_norm_t4(c):
            kat = c.get("towar_typ", "Brak danych")
            if kat == "Kolektor":
                return "Kolektor"
            if kat == "Skrzynia biegów":
                return "Skrzynia biegów"
            return "Nieprzypisane"

        def _date_t4(c):
            raw = c.get("_raw_data") or {}
            for field in ("completed_at", "started_at", "assigned_at", "archived_at"):
                val = raw.get(field)
                if val:
                    try:
                        if hasattr(val, "strftime"):
                            return val.strftime("%Y-%m-%d")
                        s = str(val)[:10]
                        if len(s) == 10 and s[4] == "-" and s[7] == "-":
                            return s
                    except Exception:
                        continue
            return None

        wanted_kat = TYP_MAP[typ_t4]
        for c in all_cases_t4:
            if not _is_pz6_t4(c):
                continue
            if _kat_norm_t4(c) != wanted_kat:
                continue
            if not is_all_t4 and c.get("operator") != picked_t4:
                continue
            d = _date_t4(c)
            if d and d in per_day_t4:
                per_day_t4[d] += 1
                period_total_t4 += 1

    # === Metryki ===
    st.markdown("---")
    avg_per_day_t4 = round(period_total_t4 / days_n_t4, 1) if days_n_t4 > 0 else 0
    mcol1, mcol2, mcol3 = st.columns(3)
    mcol1.metric("💎 PZ6 łącznie", period_total_t4)
    mcol2.metric("📅 Dni w zakresie", days_n_t4)
    mcol3.metric("📊 Średnio / dzień", avg_per_day_t4)

    if period_total_t4 == 0:
        st.warning(
            f"🔍 Brak diamentów w zakresie **{t4_from} → {t4_to}** dla **{picked_t4}** (typ: {typ_t4}). "
            f"Źródło: {'ew_cases_archived (z filtrem typu)' if use_archived else 'stats/{data} (jak tab Drill-down)'}."
        )
        st.stop()

    # === Wykres słupkowy per dzień ===
    st.markdown("---")
    if days_n_t4 > 1:
        st.markdown(f"### 📊 Diamenty per dzień — {picked_t4}")
        df_t4 = pd.DataFrame([{"Data": d, "PZ6": per_day_t4[d]} for d in dates_t4])
        st.bar_chart(df_t4.set_index("Data"), height=420)

        st.markdown("### 📈 Trend dzienny")
        st.line_chart(df_t4.set_index("Data"), height=280)
    else:
        # Jeden dzień
        st.success(f"💎 **{period_total_t4}** diamentów dla **{picked_t4}** w dniu **{t4_from.strftime('%Y-%m-%d')}** (typ: {typ_t4}).")

    # === Podsumowanie / źródło ===
    st.caption(
        f"📋 **{period_total_t4}** diamentów (PZ6) | typ: {typ_t4} | operator: {picked_t4} | "
        f"zakres: {t4_from} → {t4_to} ({days_n_t4} dni). "
        f"Źródło: **{'ew_cases_archived (PZ6 z typem)' if use_archived else 'stats/{data}.pz_transitions (jak Drill-down)'}**."
    )
