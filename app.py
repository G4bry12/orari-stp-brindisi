# -*- coding: utf-8 -*-
"""Consultazione orari STP Brindisi - applicazione Streamlit.

Sezione A (Consultazione Singola Linea): Tabellone Completo, Cerca per Fermata
Sezione B (Ricerca Globale & Bot Itinerari): Trova Linea, Passaggi alla Fermata

Avvio: python -m streamlit run app.py
"""

import glob
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ------------------------------------------------------------------ Config
FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orari_json")
TZ = ZoneInfo("Europe/Rome")
WEEKDAYS_IT = [
    "lunedì", "martedì", "mercoledì", "giovedì",
    "venerdì", "sabato", "domenica",
]

st.set_page_config(page_title="Orari STP Brindisi", page_icon="bus", layout="wide")


# ------------------------------------------------------------------ Helpers Orario
def to_min(t):
    """Converte "HH:MM" in minuti dalla mezzanotte (gestisce orari > 24h)."""
    if not t or not isinstance(t, str):
        return None
    try:
        h, m = map(int, t.split(":"))
        return h * 60 + m if 0 <= m <= 59 and h <= 47 else None
    except (ValueError, TypeError):
        return None


def next_occurrence_min(t, anchor):
    """Prossima occorrenza di t (in minuti) rispetto a anchor (minuti assoluti).

    Se l'orario e' gia' passato oggi, ritorna domani (+1440).
    """
    m = to_min(t)
    if m is None:
        return None
    day = m % 1440
    return day if day >= anchor else day + 1440


def imminent_badge(occ, anchor, window=15):
    """Badge di stato per corsa imminente (entro 'window' minuti)."""
    if occ is None:
        return ""
    diff = occ - anchor
    if diff == 0:
        return "In arrivo ora"
    if 0 < diff <= window:
        return f"Tra {diff} min"
    return ""


def fmt_hm(total_min):
    """Minuti assoluti -> "HH:MM" nel formato orario 24 ore."""
    return f"{(total_min % 1440) // 60:02d}:{total_min % 60:02d}"


# ------------------------------------------------------------------ Helpers Etichette
def linea_label(doc):
    ln = doc.get("linea") or "?"
    nome = doc.get("nome") or ""
    return f"Linea {ln} -- {nome}" if nome else f"Linea {ln}"


def tabella_label(tb):
    g = tb.get("giorni_validita") or "(giorni non indicati)"
    d = tb.get("direzione")
    return f"{g} . {d}" if d else g


def dir_label(tb):
    return tb.get("direzione") or "Circolare"


def corsa_label(idx, corsa):
    n, r = corsa.get("numero_corsa"), corsa.get("restrizione")
    lab = str(n) if n else f"corsa {idx}"
    return f"{lab} {r}".strip() if r else lab


# ------------------------------------------------------------------ Helpers Scoring
def table_score(tb, weekday):
    """Affinita' della tabella rispetto al giorno della settimana (0 = nessuna)."""
    g = (tb.get("giorni_validita") or "").lower()
    if weekday == 6:
        return 4 if "festiv" in g else (3 if "domenica" in g else 0)
    if weekday == 5:
        if "festiv" in g or "domenica" in g:
            return 0
        return 4 if "sabato" in g else (3 if "feriale" in g else 1)
    if "festiv" in g or ("domenica" in g and "sabato" not in g):
        return 0
    if "venerd" in g:
        return 4
    return 3 if "feriale" in g else (2 if "sabato" in g else 1)


def default_table(tabelle, weekday):
    """Seleziona la tabella ideale per il giorno corrente."""
    if not tabelle:
        return None
    return max(tabelle, key=lambda t: table_score(t, weekday), default=tabelle[0])


# ------------------------------------------------------------------ Data Loaders
@st.cache_data(show_spinner="Carico gli orari...")
def load_docs():
    docs = []
    for path in sorted(glob.glob(os.path.join(FOLDER, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                docs.append({"base": os.path.splitext(os.path.basename(path))[0], **json.load(f)})
        except (json.JSONDecodeError, OSError):
            continue
    return docs


@st.cache_data(show_spinner="Indicizzo le fermate...")
def build_stop_index(docs):
    index = {}
    for doc in docs:
        for tb in doc.get("tabelle") or []:
            fermate = tb.get("fermate") or []
            giorni, dl = tb.get("giorni_validita") or "(non indicato)", dir_label(tb)
            for fi, fname in enumerate(fermate):
                if not fname or len(fname) > 90:
                    continue
                # Escludi descrizioni di linea / tragitti (es. "Linea 9 - partenza da capolinea")
                if fname.startswith("Linea "):
                    continue
                for ci, c in enumerate(tb.get("corse") or [], 1):
                    t = (c.get("orari") or [None] * len(fermate))[fi]
                    if t and to_min(t) is not None:
                        index.setdefault(fname, []).append({
                            "linea": str(doc.get("linea") or doc["base"]),
                            "percorso": doc.get("nome") or "",
                            "giorni": giorni,
                            "direzione": dl,
                            "corsa": c.get("numero_corsa") or f"#{ci}",
                            "restrizione": c.get("restrizione") or "",
                            "orario": t,
                        })
    for recs in index.values():
        recs.sort(key=lambda r: to_min(r["orario"]))
    return index


# ------------------------------------------------------------------ Helpers Bot "Trova Linea"
def _norm(s):
    """Normalizza un nome fermata (spazi, case)."""
    return " ".join((s or "").split()).casefold()


def _aliases(s):
    """Alias di una fermata: rimuove prefissi direzionali 'da '/'a ' e
    il prefisso cittadino 'brindisi - ' per facilitare il matching.
    """
    n = _norm(s)
    al = {n}
    for p in ("da ", "a ", "brindisi - "):
        if n.startswith(p):
            al.add(n[len(p):])
    return al


def find_direct_services(docs, da, a, anchor, weekday=None):
    """Trova le linee dirette che collegano 'da' ad 'a' nello stesso tabellone.

    Se weekday e' specificato, considera solo le tabelle compatibili con quel
    giorno (table_score > 0).
    Gestisce fermate multiple (linee circolari / anelli): se una fermata
    compare piu' volte nella stessa lista fermate, verifica tutte le coppie
    di indici (ia, id) tali che ia < id e sceglie il tragitto migliore
    (tempo di viaggio minimo) per ogni corsa.
    """
    aliases_da, aliases_a = _aliases(da), _aliases(a)
    services = []
    for doc in docs:
        for tb in doc.get("tabelle") or []:
            if weekday is not None and table_score(tb, weekday) == 0:
                continue
            fermate = tb.get("fermate") or []
            if not fermate or len(fermate) > 40:
                continue
            # Tutti gli indici che corrispondono alla partenza/arrivo
            ias = [i for i, f in enumerate(fermate)
                   if f and _aliases(f) & aliases_da]
            ids = [i for i, f in enumerate(fermate)
                   if f and _aliases(f) & aliases_a]
            if not ias or not ids:
                continue
            # Coppie valide: ia < id (stessa direzione lungo il percorso)
            coppie = [(ia, id_) for ia in ias for id_ in ids if ia < id_]
            if not coppie:
                continue
            runs = []
            for ci, c in enumerate(tb.get("corse") or [], 1):
                orari = c.get("orari") or [None] * len(fermate)
                # Per ogni corsa, scegli la coppia con tempo di viaggio minimo
                best_pair = None
                for ia, id_ in coppie:
                    ma, md = to_min(orari[ia]), to_min(orari[id_])
                    if ma is None or md is None:
                        continue
                    dt = (md - ma) % 1440
                    if dt > 120:
                        continue
                    if best_pair is None or dt < best_pair["dt"]:
                        best_pair = {
                            "dt": dt, "ma": ma, "md": md,
                            "ta": orari[ia], "td": orari[id_],
                            "ia": ia, "id_": id_,
                        }
                if best_pair is None:
                    continue
                ta = best_pair["ta"]
                occ = next_occurrence_min(ta, anchor)
                runs.append({
                    "orario": ta,
                    "occ": occ if occ is not None else best_pair["ma"] + 1440,
                    "orario_arrivo": best_pair["td"],
                    "corsa": c.get("numero_corsa") or f"#{ci}",
                    "restrizione": c.get("restrizione") or "",
                })
            if not runs:
                continue
            runs.sort(key=lambda r: r["occ"])
            best_run = runs[0]
            services.append({
                "linea": str(doc.get("linea") or doc["base"]),
                "percorso": doc.get("nome") or "",
                "giorni": tb.get("giorni_validita") or "(non indicato)",
                "direzione": dir_label(tb),
                "tabella": tb,
                "partenza": best_run,
                "arrivo": {"orario": best_run["orario_arrivo"]},
                "viaggio": (to_min(best_run["orario_arrivo"])
                            - to_min(best_run["orario"])) % 1440,
                "attesa": best_run["occ"] - anchor,
            })
    services.sort(key=lambda s: (s["attesa"], s["linea"]))
    return services


def _build_hub_index(docs, min_lines=2):
    """Costruisce l'indice degli scali: fermate servite da >= min_lines linee."""
    stop_lines = {}
    for doc in docs:
        ln = str(doc.get("linea") or "?")
        for tb in doc.get("tabelle") or []:
            for f in tb.get("fermate") or []:
                if not f or f.startswith("Linea "):
                    continue
                norm = _norm(f)
                if norm not in stop_lines:
                    stop_lines[norm] = {"nome": f, "linee": set()}
                stop_lines[norm]["linee"].add(ln)
    return {n: info for n, info in stop_lines.items()
            if len(info["linee"]) >= min_lines}


def find_transfer_services(docs, da, a, anchor, weekday=None, top=3):
    """Cerca itinerari con cambio a uno scalo quando non c'e' la diretta.

    Per ogni scalo: verifica se una linea serve da->scalo e un'altra
    serve scalo->a, poi calcola l'orario migliore (partenza + arrivo
    stimato allo scalo + attesa cambio + arrivo finale).
    """
    if _norm(da) == _norm(a):
        return []

    hub_index = _build_hub_index(docs, min_lines=3)
    aliases_da, aliases_a = _aliases(da), _aliases(a)

    # Trova tutte le linee/fermate che servono da e a
    def _stop_matches(stop_name, aliases):
        """Trova il nome canonico della fermata che corrisponde agli alias."""
        for canonical in hub_index:
            if _aliases(canonical) & aliases:
                return canonical
        return None

    results = []
    for hub_norm, hub_info in hub_index.items():
        hub_name = hub_info["nome"]
        aliases_hub = _aliases(hub_name)

        # 1) Cerca linee da -> scalo
        leg1_services = []
        for doc in docs:
            ln = str(doc.get("linea") or "?")
            for tb in doc.get("tabelle") or []:
                if weekday is not None and table_score(tb, weekday) == 0:
                    continue
                fermate = tb.get("fermate") or []
                if not fermate or len(fermate) > 40:
                    continue
                ias = [i for i, f in enumerate(fermate)
                       if f and _aliases(f) & aliases_da]
                ihubs = [i for i, f in enumerate(fermate)
                         if f and _aliases(f) & aliases_hub]
                if not ias or not ihubs:
                    continue
                coppie = [(ia, ih) for ia in ias for ih in ihubs if ia < ih]
                if not coppie:
                    continue
                for ci, c in enumerate(tb.get("corse") or [], 1):
                    orari = c.get("orari") or [None] * len(fermate)
                    best = None
                    for ia, ih in coppie:
                        ma, mh = to_min(orari[ia]), to_min(orari[ih])
                        if ma is None or mh is None:
                            continue
                        dt = (mh - ma) % 1440
                        if dt > 120:
                            continue
                        if best is None or dt < best["dt"]:
                            best = {"dt": dt, "ta": orari[ia],
                                    "th": orari[ih], "ma": ma, "mh": mh}
                    if best:
                        occ = next_occurrence_min(best["ta"], anchor)
                        leg1_services.append({
                            "linea": ln, "giorni": tb.get("giorni_validita"),
                            "direzione": dir_label(tb),
                            "corsa": c.get("numero_corsa") or f"#{ci}",
                            "ta": best["ta"], "th": best["th"],
                            "ma": best["ma"], "mh": best["mh"],
                            "dt": best["dt"],
                            "occ_partenza": occ or best["ma"] + 1440,
                        })

        if not leg1_services:
            continue

        # 2) Cerca linee scalo -> a
        leg2_services = []
        for doc in docs:
            ln = str(doc.get("linea") or "?")
            for tb in doc.get("tabelle") or []:
                if weekday is not None and table_score(tb, weekday) == 0:
                    continue
                fermate = tb.get("fermate") or []
                if not fermate or len(fermate) > 40:
                    continue
                ihubs = [i for i, f in enumerate(fermate)
                         if f and _aliases(f) & aliases_hub]
                iarr = [i for i, f in enumerate(fermate)
                        if f and _aliases(f) & aliases_a]
                if not ihubs or not iarr:
                    continue
                coppie = [(ih, ia) for ih in ihubs for ia in iarr if ih < ia]
                if not coppie:
                    continue
                for ci, c in enumerate(tb.get("corse") or [], 1):
                    orari = c.get("orari") or [None] * len(fermate)
                    best = None
                    for ih, ia in coppie:
                        mh, ma = to_min(orari[ih]), to_min(orari[ia])
                        if mh is None or ma is None:
                            continue
                        dt = (ma - mh) % 1440
                        if dt > 120:
                            continue
                        if best is None or dt < best["dt"]:
                            best = {"dt": dt, "th": orari[ih],
                                    "ta": orari[ia], "mh": mh, "ma": ma}
                    if best:
                        leg2_services.append({
                            "linea": ln, "giorni": tb.get("giorni_validita"),
                            "direzione": dir_label(tb),
                            "corsa": c.get("numero_corsa") or f"#{ci}",
                            "th": best["th"], "ta": best["ta"],
                            "mh": best["mh"], "ma": best["ma"],
                            "dt": best["dt"],
                        })

        if not leg2_services:
            continue

        # 3) Combina: per ogni leg1, trova il leg2 migliore che parte
        #    dopo l'arrivo allo scalo + tempo di cambio (5 min)
        SCAMBO_MIN = 5
        for l1 in leg1_services:
            arrivo_scalo = l1["mh"]  # minuti assoluti arrivo allo scalo
            # Cerca leg2 che parte dopo arrivo_scalo + SCAMBO_MIN
            best_l2 = None
            for l2 in leg2_services:
                partenza_l2 = l2["mh"]  # minuti assoluti partenza dallo scalo
                # Stesso giorno o domani?
                attesa = (partenza_l2 - arrivo_scalo) % 1440
                if attesa < SCAMBO_MIN:
                    attesa += 1440
                tempo_totale = l1["dt"] + attesa + l2["dt"]
                if best_l2 is None or tempo_totale < best_l2["tempo_totale"]:
                    best_l2 = {
                        "l2": l2, "attesa": attesa,
                        "tempo_totale": tempo_totale,
                    }
            if best_l2 is None:
                continue
            l2 = best_l2["l2"]
            attesa = best_l2["attesa"]
            tempo_totale = best_l2["tempo_totale"]

            # Calcola orari formattati
            occ_p = l1["occ_partenza"]
            arr_scalo_fmt = fmt_hm(arrivo_scalo)
            part_scalo_fmt = fmt_hm(arrivo_scalo + attesa)
            arr_finale = arrivo_scalo + attesa + l2["dt"]

            results.append({
                "linea1": l1["linea"],
                "linea2": l2["linea"],
                "scalo": hub_name,
                "partenza": l1["ta"],
                "arrivo_scalo": arr_scalo_fmt,
                "partenza_scalo": part_scalo_fmt,
                "arrivo_finale": fmt_hm(arr_finale),
                "giorni1": l1["giorni"],
                "giorni2": l2["giorni"],
                "corsa1": l1["corsa"],
                "corsa2": l2["corsa"],
                "tempo_totale": tempo_totale,
                "attesa_scambio": attesa,
                "occ_partenza": occ_p,
            })

    # Deduplica per scalo + linee (tieni il miglior tempo)
    seen = {}
    for r in results:
        # Normalizza lo scalo: rimuovi prefissi per confronto
        hub_key = _norm(r["scalo"])
        for p in ("da ", "a ", "brindisi - "):
            if hub_key.startswith(p):
                hub_key = hub_key[len(p):]
        key = (r["linea1"], r["linea2"], hub_key)
        if key not in seen or r["tempo_totale"] < seen[key]["tempo_totale"]:
            seen[key] = r
    deduped = list(seen.values())
    deduped.sort(key=lambda r: r["tempo_totale"])
    return deduped[:top]


def suggest_via_hub(docs, da, a, direct, top=3):
    """Suggerisce linee uniche presenti in tabelle diverse quando non vi sono corse dirette."""
    if _norm(da) == _norm(a):
        return []
    index = build_stop_index(docs)

    def _ix(name):
        recs = index.get(name)
        if recs:
            return recs
        an = _aliases(name)
        return [v for k, rec_list in index.items()
                if _aliases(k) & an for v in rec_list]

    skip = {s["linea"] for s in direct}
    others = [r for r in _ix(da) if r["linea"] not in skip]
    others_d = [r for r in _ix(a) if r["linea"] not in skip]
    if not others or not others_d:
        return []

    count = {}
    for r in others + others_d:
        count[r["linea"]] = count.get(r["linea"], 0) + 1

    sug = []
    for linea in sorted(count, key=lambda k: (-count[k], k)):
        leg1 = next((r for r in others if r["linea"] == linea), None)
        leg2 = next((r for r in others_d if r["linea"] == linea), None)
        if leg1 and leg2:
            sug.append({"linea": linea, "p1": leg1, "p2": leg2})
        if len(sug) >= top:
            break
    return sug


# ------------------------------------------------------------------ UI Components
def render_upcoming_metrics(upcoming, anchor, is_global=False):
    """Rende i badge metrici per le corse imminenti."""
    cols = st.columns(min(3, len(upcoming)))
    for col, r in zip(cols, upcoming[:3]):
        diff = r["occ"] - anchor
        badge = ("In arrivo ora" if diff == 0
                 else (f"Tra {diff} min" if diff <= 15 else "Prossima"))
        if is_global:
            col.metric(f"{badge} . Linea {r['Linea']}",
                       r["Orario"],
                       delta=f"corsa {r['Corsa']}" if r["Corsa"] else None,
                       delta_color="off")
        else:
            col.metric(f"{badge} . corsa {r['Corsa']}".strip(),
                       r["Orario"])


# ------------------------------------------------------------------ State & Callbacks
def _on_line_change():
    tabs = st.session_state["linea_sel"].get("tabelle") or []
    tb0 = default_table(tabs, datetime.now(TZ).weekday())
    if tb0:
        st.session_state["tab_sel"] = tb0
        st.session_state["dirq"] = dir_label(tb0)


def _on_tab_change():
    if st.session_state["tab_sel"]:
        st.session_state["dirq"] = dir_label(st.session_state["tab_sel"])


def _on_dir_change():
    tb_cur = st.session_state["tab_sel"]
    for t in st.session_state["linea_sel"].get("tabelle") or []:
        if (t.get("giorni_validita") == tb_cur.get("giorni_validita")
                and dir_label(t) == st.session_state["dirq"]):
            st.session_state["tab_sel"] = t
            break


# ------------------------------------------------------------------ Sezione A: Consultazione Singola Linea
def render_sezione_a(docs, today):
    """Renderizza la Sezione A: Tabellone Completo + Cerca per Fermata."""
    now_min = today.hour * 60 + today.minute

    # --- Sidebar: selezione linea/tabella
    doc = st.sidebar.selectbox("Linea", docs, format_func=linea_label,
                               key="linea_sel", on_change=_on_line_change)
    tabelle = doc.get("tabelle") or []
    if not tabelle:
        st.error("Questo file non contiene tabelle orario.")
        return

    if ("tab_sel" not in st.session_state
            or st.session_state["tab_sel"] not in tabelle):
        st.session_state["tab_sel"] = default_table(tabelle, today.weekday())
    if "dirq" not in st.session_state:
        st.session_state["dirq"] = dir_label(st.session_state["tab_sel"])

    tb = st.sidebar.selectbox("Tabella / Giorni di validita",
                               tabelle, format_func=tabella_label,
                               key="tab_sel", on_change=_on_tab_change)
    st.sidebar.divider()
    st.sidebar.caption(f"Fonte: `{doc['base']}.pdf`")

    # --- Intestazione
    st.title(f"Linea {doc.get('linea') or 'n.d.'}")
    if doc.get("nome"):
        st.subheader(doc["nome"])
    st.caption(f"**{tabella_label(tb)}** . oggi e' {WEEKDAYS_IT[today.weekday()]}")

    if doc.get("note"):
        with st.expander("Note e deviazioni della linea", expanded=False):
            for n in filter(None, doc["note"]):
                st.markdown(f"- {n}")

    # --- Filtro rapido direzione
    same_giorni = [t for t in tabelle
                  if t.get("giorni_validita") == tb.get("giorni_validita")]
    dirs = sorted({dir_label(t) for t in same_giorni})
    if len(same_giorni) > 1 and len(dirs) > 1:
        if st.session_state["dirq"] not in dirs:
            st.session_state["dirq"] = dir_label(tb)
        st.radio("Direzione", dirs, horizontal=True,
                 key="dirq", on_change=_on_dir_change)

    fermate, corse = tb.get("fermate") or [], tb.get("corse") or []
    if not fermate or not corse:
        st.warning("Tabella senza dati sufficienti.")
        return

    # --- Tabs Sezione A
    tab1, tab2 = st.tabs(["Tabellone Completo", "Cerca per Fermata"])

    # --- Tab: Tabellone Completo
    with tab1:
        data = {
            corsa_label(i, c): [t if t is not None else "-"
                                for t in c.get("orari") or []]
            for i, c in enumerate(corse, 1)
        }
        df = pd.DataFrame(data, index=[f or "(vedi note)" for f in fermate])
        df.index.name = "Fermata"
        st.dataframe(df, width="stretch", height=min(950, 42 * len(df) + 38))
        st.download_button(
            "Scarica questa tabella (JSON)",
            data=json.dumps(tb, ensure_ascii=False, indent=2),
            file_name=f"{doc['base']}_tabella.json",
            mime="application/json",
        )

    # --- Tab: Cerca per Fermata
    with tab2:
        options = [(i, f) for i, f in enumerate(fermate) if f is not None]
        if not options:
            st.warning("Nessuna fermata disponibile.")
            return

        fi, fname = st.selectbox("Fermata", options,
                                 format_func=lambda x: x[1], index=0)

        rows = []
        for i, c in enumerate(corse, 1):
            t = (c.get("orari") or [None] * len(fermate))[fi]
            if t:
                occ = next_occurrence_min(t, now_min)
                rows.append({
                    "m": to_min(t) or 10_000,
                    "occ": occ or 10_000,
                    "Orario": t,
                    "Corsa": c.get("numero_corsa") or f"#{i}",
                    "Restrizione": c.get("restrizione") or "",
                    "Stato": imminent_badge(occ, now_min),
                })

        rows.sort(key=lambda x: (x["m"], x["Orario"]))
        st.markdown(f"### {fname}")

        upcoming = [r for r in rows if r["occ"] >= now_min]
        if upcoming:
            render_upcoming_metrics(upcoming, now_min)
            st.caption(
                f"Ora corrente: {today.strftime('%H:%M')} "
                f"- corse successive: {len(upcoming)} su {len(rows)} totali."
            )
        elif rows:
            st.info(f"Nessuna corsa successiva alle {today.strftime('%H:%M')} "
                    f"per questa fermata.")

        display_df = pd.DataFrame(rows).drop(columns=["m", "occ"])
        st.dataframe(display_df, width="stretch", hide_index=True)


# ------------------------------------------------------------------ Sezione B: Ricerca Globale & Bot Itinerari
def render_sezione_b(docs, today):
    """Renderizza la Sezione B: Trova Linea (bot) + Passaggi alla Fermata."""
    now_min = today.hour * 60 + today.minute

    # --- Sidebar: info dataset
    index = build_stop_index(docs)
    st.sidebar.info(
        f"Dataset: {len(docs)} linee, {len(index)} fermate uniche"
    )

    # --- Tabs Sezione B
    tab_bot, tab_passaggi = st.tabs(["Trova Linea", "Passaggi alla Fermata"])

    # ================================================================== Tab: Trova Linea
    with tab_bot:
        st.caption(
            "Inserisci provenienza e destinazione: il bot cerca la linea diretta "
            "con orario previsto. Per le linee circolari, verifica tutte le "
            "possibili traiettorie lungo lo stesso tabellone."
        )
        all_stops = sorted(index)

        # Giorno di partenza
        idx_oggi = today.weekday()
        giorno_sel = st.selectbox(
            "Giorno di partenza",
            WEEKDAYS_IT,
            index=idx_oggi,
            key="bot_giorno",
        )
        selected_weekday = WEEKDAYS_IT.index(giorno_sel)

        # Orario di partenza desiderato
        anchor_time = st.time_input(
            "Orario di partenza desiderato",
            value=today.time(),
            key="bot_ora",
        )
        anchor_min = anchor_time.hour * 60 + anchor_time.minute

        # Provenienza / Destinazione (pulsante inverti PRIMA dei selettori)
        ca, cb, cx = st.columns([5, 5, 0.7])
        if cx.button("Inverti", help="Inverti provenienza e destinazione"):
            st.session_state["bot_da"] = st.session_state.get("bot_a")
            st.session_state["bot_a"] = st.session_state.get("bot_da")

        da = ca.selectbox("Provenienza", all_stops, index=None,
                          key="bot_da", placeholder="Dove parti?")
        a = cb.selectbox("Destinazione", all_stops, index=None,
                         key="bot_a", placeholder="Dove vuoi andare?")

        if not da or not a:
            st.info("Seleziona provenienza e destinazione per iniziare.")
        elif da == a:
            st.warning("Provenienza e destinazione coincidono.")
        else:
            direct = find_direct_services(docs, da, a, anchor_min,
                                         selected_weekday)

            if direct:
                best = direct[0]
                occ = best["partenza"]["occ"]
                attesa = best["attesa"]
                attesa_str = (f"fra {attesa} min" if attesa < 60
                              else f"fra {attesa // 60}h {attesa % 60}min")
                st.success(
                    f"Prendi la **Linea {best['linea']}** - "
                    f"corsa delle **{best['partenza']['orario']}** "
                    f"({best['giorni']}) da **{da}** fino a **{a}** "
                    f"(arrivo previsto **{fmt_hm(occ + best['viaggio'])}**)."
                )
                st.caption(f"Percorso linea: {best['percorso']}")

                m1, m2, m3 = st.columns(3)
                m1.metric("Partenza", best["partenza"]["orario"],
                          delta=imminent_badge(occ, anchor_min) or attesa_str,
                          delta_color="off")
                m2.metric("Arrivo", fmt_hm(occ + best["viaggio"]),
                          delta="stesso autobus", delta_color="off")
                m3.metric("A bordo", f"{best['viaggio']} min")

                extra = direct[1:]
                if extra:
                    with st.expander(f"Altre {len(extra)} opzioni dirette",
                                     expanded=False):
                        rows = [{
                            "Linea": s["linea"],
                            "Partenza": s["partenza"]["orario"],
                            "Arrivo": fmt_hm(s["partenza"]["occ"] + s["viaggio"]),
                            "A bordo (min)": s["viaggio"],
                            "Giorni": s["giorni"],
                            "Direzione": s["direzione"],
                            "Corsa": s["partenza"]["corsa"],
                        } for s in extra]
                        st.dataframe(pd.DataFrame(rows),
                                     width="stretch", hide_index=True)
            else:
                st.warning(
                    f"Nessuna linea collega direttamente **{da}** a **{a}** "
                    f"in un unico tabellone."
                )

                # --- Scali: itinerari con cambio ---
                transfers = find_transfer_services(
                    docs, da, a, anchor_min, selected_weekday, top=5)
                if transfers:
                    st.markdown("**🔀 Itinerari con cambio (scalo):**")
                    for i, tr in enumerate(transfers):
                        with st.expander(
                            f"Linea {tr['linea1']} → {tr['linea2']} "
                            f"via **{tr['scalo']}** "
                            f"({tr['tempo_totale']} min totali)",
                            expanded=(i == 0),
                        ):
                            c1, c2, c3 = st.columns(3)
                            c1.metric(
                                f"Linea {tr['linea1']}",
                                f"parte {tr['partenza']}",
                                delta=f"corsa {tr['corsa1']}",
                                delta_color="off",
                            )
                            c2.metric(
                                f"⏱ Scalo {tr['scalo']}",
                                f"arrivo {tr['arrivo_scalo']}"
                                f" → partenza {tr['partenza_scalo']}",
                                delta=f"attesa {tr['attesa_scambio']} min",
                                delta_color="off",
                            )
                            c3.metric(
                                f"Linea {tr['linea2']}",
                                f"arrivo {tr['arrivo_finale']}",
                                delta=f"corsa {tr['corsa2']}",
                                delta_color="off",
                            )
                            st.caption(
                                f"{tr['giorni1']} / {tr['giorni2']} "
                                f"• {tr['tempo_totale']} min totali"
                            )

                # --- Suggerimento linee in tabelle diverse ---
                sug = suggest_via_hub(docs, da, a, direct, top=3)
                if sug:
                    st.markdown(
                        "**🚌 Linee che servono entrambe le fermate "
                        "(in tabelle differenti, stesso autobus):**"
                    )
                    for s in sug:
                        st.markdown(
                            f"- **Linea {s['linea']}**: da {da} "
                            f"es. {s['p1']['orario']} ({s['p1']['giorni']})"
                            f" - verso {a} es. {s['p2']['orario']} "
                            f"({s['p2']['giorni']})"
                        )

                if not transfers and not sug:
                    st.info(
                        "Nessun itinerario trovato. Prova con fermate "
                        "principali (es. Stazione, Capolinea)."
                    )

    # ================================================================== Tab: Passaggi alla Fermata
    with tab_passaggi:
        st.caption(
            "Tutti i passaggi su una fermata, di tutte le linee del dataset, "
            "in ordine cronologico."
        )

        # Orario di partenza desiderato
        pass_time = st.time_input(
            "Orario di partenza desiderato",
            value=today.time(),
            key="ora_passaggi",
        )
        pass_anchor = pass_time.hour * 60 + pass_time.minute

        stop = st.selectbox(
            "Fermata (ricerca in tutte le linee)",
            sorted(index), index=None,
            placeholder="Seleziona o digita una fermata...",
        )
        if not stop:
            st.info("Seleziona una fermata per vedere tutti i passaggi.")
            return

        recs = index[stop]
        giorni_all = sorted({r["giorni"] for r in recs})

        if "gg_globale" in st.session_state:
            st.session_state["gg_globale"] = [
                g for g in st.session_state["gg_globale"]
                if g in giorni_all
            ]
        giorni_sel = st.multiselect(
            "Filtra per giorni di validita",
            giorni_all, default=giorni_all, key="gg_globale",
        )

        rows = []
        for r in [r for r in recs if r["giorni"] in giorni_sel]:
            occ = next_occurrence_min(r["orario"], pass_anchor)
            rows.append({
                "m": to_min(r["orario"]) or 10_000,
                "occ": occ or 10_000,
                "Orario": r["orario"],
                "Linea": r["linea"],
                "Giorni": r["giorni"],
                "Direzione": r["direzione"],
                "Corsa": r["corsa"],
                "Restrizione": r["restrizione"],
                "Stato": imminent_badge(occ, pass_anchor),
            })

        rows.sort(key=lambda x: (x["m"], x["Linea"], x["Giorni"]))

        upcoming = [r for r in rows if r["occ"] >= pass_anchor]
        if upcoming:
            render_upcoming_metrics(upcoming, pass_anchor, is_global=True)

        st.markdown(f"### {stop}")
        display_df = pd.DataFrame(rows).drop(columns=["m", "occ"])
        st.dataframe(
            display_df, width="stretch",
            height=min(700, 42 * (len(display_df) + 1) + 38),
            hide_index=True,
        )
        st.caption(
            f"Riferimento orario: {pass_time.strftime('%H:%M')} "
            f"- le corse indicate partono da quell'ora in poi."
        )


# ------------------------------------------------------------------ Main
docs = load_docs()
if not docs:
    st.error(f"Nessun file JSON trovato nella cartella `{FOLDER}`.")
    st.stop()

today = datetime.now(TZ)

# --- Sidebar: selettore macro-sezione
st.sidebar.title("Orari STP Brindisi")
sezione = st.sidebar.radio(
    "Sezione",
    ["Consultazione Singola Linea", "Ricerca Globale & Bot Itinerari"],
    key="sezione",
    label_visibility="collapsed",
)

# --- Dispatch
if sezione.startswith("Consultazione"):
    render_sezione_a(docs, today)
else:
    render_sezione_b(docs, today)
