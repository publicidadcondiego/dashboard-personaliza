#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Personaliza Inmobiliaria — Generador automático del dashboard diario.

Fuentes:
  1. Windsor.ai  → Meta Ads (inversión, leads, CPL, anuncios, placements)
  2. Google Sheets → Hoja 3 (embudo comercial: contacto → separación)

Corre en GitHub Actions una vez al día.
Secretos del repo:
  WINDSOR_API_KEY   → API key de Windsor
  GOOGLE_CREDS_JSON → JSON de cuenta de servicio de Google (base64 o raw)
  SHEET_ID          → ID del Google Sheet (o hardcodeado abajo)
"""

import os
import json
import base64
import datetime as dt
import tempfile
from urllib.parse import urlencode
from urllib.request import urlopen, Request

# ─────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────
WINDSOR_API_KEY  = os.environ.get("WINDSOR_API_KEY", "")
GOOGLE_CREDS_RAW = os.environ.get("GOOGLE_CREDS_JSON", "")   # JSON de service account
SHEET_ID         = os.environ.get("SHEET_ID",
                   "1RoKIByz4e257VW5WSeSUGHza1s0cPrDAoDK7gzeHusY")
SHEET_TAB        = "Hoja 3"

META_ACCOUNT     = "479389551849832"
CONNECTOR        = "facebook"
CAMPAIGN_ID      = "120251374160960079"   # CP Pueblo Libre
CAMPAIGN_LABEL   = "CP Pueblo Libre"
CLIENT_NAME      = "Personaliza Inmobiliaria"
CAMPAIGN_START   = dt.date(2026, 6, 20)   # inicio del ciclo

CURRENCY_META    = "USD"
SOLES_RATE       = 3.75                   # referencial USD → S/

OUTPUT_PATH      = "index.html"
HISTORY_PATH     = "data/history.json"

API_BASE         = "https://connectors.windsor.ai"

# ─────────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────────
def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0

def fmt_usd(v):
    return f"${v:,.2f}"

def fmt_int(v):
    return f"{int(v):,}".replace(",", ".")

# ─────────────────────────────────────────────────────────────
# 1. WINDSOR — datos de Meta Ads
# ─────────────────────────────────────────────────────────────
def windsor_get(fields, date_from, date_to, extra=None):
    if not WINDSOR_API_KEY:
        raise SystemExit("Falta WINDSOR_API_KEY")
    import json as _json
    filters = _json.dumps([["campaign_id", "eq", CAMPAIGN_ID]])
    params = {
        "api_key":   WINDSOR_API_KEY,
        "connector":  CONNECTOR,
        "accounts":  META_ACCOUNT,
        "date_from": date_from.isoformat(),
        "date_to":   date_to.isoformat(),
        "_renderer": "json",
        "filters":   filters,
    }
    if extra:
        params.update(extra)
    # Construir query string manualmente para que 'fields' use comas literales
    fields_str = ",".join(fields)
    base_qs = urlencode(params)
    url = f"{API_BASE}/{CONNECTOR}?{base_qs}&fields={fields_str}"
    req = Request(url, headers={"User-Agent": "dashboard-bot/1.0"})
    with urlopen(req, timeout=120) as r:
        payload = json.loads(r.read().decode("utf-8"))
    return payload.get("data", payload if isinstance(payload, list) else [])


def fetch_meta(date_from, date_to):
    """Trae todos los segmentos que necesita el dashboard."""
    # Totales diarios
    daily = windsor_get(
        ["date", "spend", "impressions", "clicks", "actions_lead", "reach", "cpm", "ctr", "cpc"],
        date_from, date_to
    )
    daily = sorted(daily, key=lambda r: r.get("date", ""))

    # Por anuncio (acumulado)
    by_ad = windsor_get(
        ["ad_name", "spend", "impressions", "ctr", "cpm", "actions_lead"],
        date_from, date_to
    )

    # Por plataforma (Facebook vs Instagram)
    by_place = windsor_get(
        ["publisher_platform", "spend", "actions_lead"],
        date_from, date_to
    )

    return daily, by_ad, by_place


# ─────────────────────────────────────────────────────────────
# 2. GOOGLE SHEETS — embudo comercial
# ─────────────────────────────────────────────────────────────
def fetch_sheet():
    """
    Lee la Hoja 3 del Sheet de Personaliza.
    Retorna lista de dicts con las columnas del embudo.
    Usa google-auth + googleapiclient si están disponibles;
    si no, usa la Sheets API REST con service account JWT.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        # El JSON puede venir como raw string o base64
        creds_str = GOOGLE_CREDS_RAW
        if not creds_str:
            raise SystemExit("Falta GOOGLE_CREDS_JSON")
        try:
            # Intentar decodificar base64
            creds_str = base64.b64decode(creds_str).decode("utf-8")
        except Exception:
            pass  # ya es raw JSON

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(creds_str)
            creds_file = f.name

        creds = service_account.Credentials.from_service_account_file(
            creds_file,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A2:S"  # fila 2 = encabezados, 3+ = datos
        ).execute()
        rows = result.get("values", [])

    except ImportError:
        # Fallback: leer sin autenticación si el sheet es público
        # (útil para pruebas; en producción siempre usar service account)
        print("AVISO: google-auth no instalado. Intentando leer Sheet como público...")
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
            f"/values/{SHEET_TAB}!A2:S?key={os.environ.get('GOOGLE_API_KEY','')}"
        )
        req = Request(url, headers={"User-Agent": "dashboard-bot/1.0"})
        with urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        rows = data.get("values", [])

    # Fila 2 del sheet = encabezados (índice 0 en la respuesta cuando range empieza en A2)
    if not rows:
        return []

    # Los encabezados son la primera fila devuelta
    headers = rows[0]
    # Normalizar nombres de columna
    col = {h.strip(): i for i, h in enumerate(headers)}

    leads = []
    for row in rows[1:]:
        def get(name, default=""):
            i = col.get(name)
            return row[i].strip() if i is not None and i < len(row) else default

        leads.append({
            "fecha":      get("B      ") or get("B"),
            "origen":     get("ORIGEN"),
            "pieza":      get("PIEZA"),
            "depto":      get("DEPARTAMENTO INTERESADO"),
            "distrito":   get("DISTRITO"),
            "publico":    get("PÚBLICO"),
            "contactado": get("Contactado"),
            "contesto":   get("Contestó Contacto"),
            "agendo":     get("Agendó Visita"),
            "llego":      get("LLegó a visitar CASETA"),
            "banco":      get("Evaluacion Banco"),
            "separacion": get("Separacion"),
            "semaforo":   get("Semáforo"),
        })
    return leads


# ─────────────────────────────────────────────────────────────
# 3. AGREGADOS META
# ─────────────────────────────────────────────────────────────
def agg_meta(daily):
    spend   = sum(num(r.get("spend"))       for r in daily)
    leads   = sum(num(r.get("actions_lead")) for r in daily)
    impr    = sum(num(r.get("impressions")) for r in daily)
    clicks  = sum(num(r.get("clicks"))      for r in daily)
    reach   = max(num(r.get("reach", 0))    for r in daily) if daily else 0
    cpl     = spend / leads   if leads  else 0
    ctr     = clicks / impr * 100 if impr else 0
    cpc     = spend / clicks  if clicks else 0
    return dict(spend=spend, leads=leads, impressions=impr,
                clicks=clicks, reach=reach, cpl=cpl, ctr=ctr, cpc=cpc)


def agg_ads(by_ad):
    """Consolida filas con el mismo nombre de anuncio."""
    ads = {}
    for r in by_ad:
        name = r.get("ad_name", "—")
        if name not in ads:
            ads[name] = {"spend": 0, "impressions": 0, "leads": 0, "ctr_sum": 0, "n": 0}
        ads[name]["spend"]       += num(r.get("spend"))
        ads[name]["impressions"] += num(r.get("impressions"))
        ads[name]["leads"]       += num(r.get("actions_lead"))
        ads[name]["ctr_sum"]     += num(r.get("ctr"))
        ads[name]["n"]           += 1

    result = []
    for name, a in ads.items():
        cpl  = a["spend"] / a["leads"] if a["leads"] else None
        cpm  = a["spend"] / a["impressions"] * 1000 if a["impressions"] else 0
        ctr  = a["ctr_sum"] / a["n"] if a["n"] else 0
        result.append(dict(name=name, spend=a["spend"], impressions=a["impressions"],
                           leads=int(a["leads"]), cpl=cpl, cpm=cpm, ctr=ctr))
    return sorted(result, key=lambda x: x["spend"], reverse=True)


def agg_placements(by_place):
    places = {}
    for r in by_place:
        key = r.get("placement") or r.get("publisher_platform") or "Otro"
        if key not in places:
            places[key] = {"spend": 0, "leads": 0}
        places[key]["spend"] += num(r.get("spend"))
        places[key]["leads"] += num(r.get("actions_lead"))
    result = []
    for k, v in places.items():
        cpl = v["spend"] / v["leads"] if v["leads"] else None
        result.append(dict(name=k, spend=v["spend"], leads=int(v["leads"]), cpl=cpl))
    return sorted(result, key=lambda x: x["spend"], reverse=True)


# ─────────────────────────────────────────────────────────────
# 4. AGREGADOS SHEET (embudo)
# ─────────────────────────────────────────────────────────────
def agg_funnel(leads):
    si = lambda v: v.strip().lower() in ("sí", "si", "yes", "true", "1")
    total       = len(leads)
    contactado  = sum(1 for l in leads if si(l["contactado"]))
    contesto    = sum(1 for l in leads if si(l["contesto"]))
    agendo      = sum(1 for l in leads if si(l["agendo"]))
    llego       = sum(1 for l in leads if si(l["llego"]))
    banco       = sum(1 for l in leads if si(l["banco"]))
    separacion  = sum(1 for l in leads if si(l["separacion"]))
    return [
        ("Leads ingresados",     total),
        ("Contactado",           contactado),
        ("Contestó el contacto", contesto),
        ("Agendó visita",        agendo),
        ("Llegó a la caseta",    llego),
        ("Evaluación bancaria",  banco),
        ("Separación (venta)",   separacion),
    ]


def agg_by_pieza(leads):
    """Leads por anuncio/pieza desde el Sheet."""
    piezas = {}
    for l in leads:
        k = l["pieza"] or "Sin pieza"
        if k not in piezas:
            piezas[k] = {"total": 0, "sep": 0}
        piezas[k]["total"] += 1
        if l["separacion"].strip().lower() in ("sí", "si"):
            piezas[k]["sep"] += 1
    return sorted(piezas.items(), key=lambda x: x[1]["total"], reverse=True)[:8]


def agg_distritos(leads):
    dist = {}
    for l in leads:
        k = l["distrito"].strip().title() or "Sin datos"
        dist[k] = dist.get(k, 0) + 1
    return sorted(dist.items(), key=lambda x: x[1], reverse=True)[:8]


def agg_publico(leads):
    pub = {}
    for l in leads:
        k = l["publico"].strip() or "Sin datos"
        pub[k] = pub.get(k, 0) + 1
    return sorted(pub.items(), key=lambda x: x[1], reverse=True)


def agg_cohortes_mes(leads):
    """Tasa de contacto por mes de ingreso."""
    meses = {}
    for l in leads:
        fecha = l["fecha"]
        try:
            d = dt.datetime.strptime(fecha, "%d/%m/%Y").date()
            key = d.strftime("%Y-%m")
            label = d.strftime("%B %Y").capitalize()
        except Exception:
            continue
        if key not in meses:
            meses[key] = {"label": label, "total": 0, "contesto": 0}
        meses[key]["total"] += 1
        if l["contesto"].strip().lower() in ("sí", "si"):
            meses[key]["contesto"] += 1
    result = []
    for k in sorted(meses):
        m = meses[k]
        tasa = f"{m['contesto']/m['total']*100:.0f}%" if m["total"] else "—"
        result.append((m["label"], m["total"], m["contesto"], tasa))
    return result


# ─────────────────────────────────────────────────────────────
# 5. HISTÓRICO DIARIO
# ─────────────────────────────────────────────────────────────
def load_history():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_history(hist, today, meta, funnel):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    key = today.isoformat()
    hist = [h for h in hist if h.get("date") != key]
    sep = next((v for k, v in funnel if "Separación" in k), 0)
    hist.append({
        "date":    key,
        "spend":   round(meta["spend"], 2),
        "leads":   meta["leads"],
        "cpl":     round(meta["cpl"], 2),
        "sep":     sep,
    })
    hist = sorted(hist, key=lambda h: h["date"])[-90:]  # últimos 90 días
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)
    return hist


# ─────────────────────────────────────────────────────────────
# 6. HTML
# ─────────────────────────────────────────────────────────────
def js(v):
    return json.dumps(v, ensure_ascii=False)


def build_html(date_from, today, meta, ads, placements,
               funnel, piezas, distritos, publicos, cohortes, hist):

    period   = f"{date_from.strftime('%d %b')} – {today.strftime('%d %b %Y')}"
    days     = (today - date_from).days + 1
    generated = today.strftime("%d/%m/%Y")
    leads_day = meta["leads"] / days if days else 0

    # KPI cards
    sep_count = next((v for k, v in funnel if "Separación" in k), 0)
    visit_count = next((v for k, v in funnel if "Llegó" in k), 0)
    cost_sep = meta["spend"] / sep_count if sep_count else 0

    # Datos para gráfico diario (histórico)
    h_labels = [h["date"][5:] for h in hist]   # MM-DD
    h_leads  = [h["leads"]    for h in hist]
    h_cpl    = [h["cpl"]      for h in hist]
    h_spend  = [h["spend"]    for h in hist]

    # Tabla anuncios
    ads_rows = ""
    for a in ads[:12]:
        cpl_val  = f"${a['cpl']:.2f}" if a["cpl"] else "—"
        cpl_color = (
            "var(--good)" if a["cpl"] and a["cpl"] < 3
            else ("var(--bad)" if a["cpl"] and a["cpl"] > 6 else "var(--ink)")
        ) if a["cpl"] else "var(--slate)"
        top = "win" if ads and a == ads[0] else ""
        ads_rows += f"""<tr class="{top}">
          <td>{a['name']}</td>
          <td class="num">${a['spend']:.2f}</td>
          <td class="num">{a['impressions']:,.0f}</td>
          <td class="num">{a['ctr']:.2f}%</td>
          <td class="num">${a['cpm']:.2f}</td>
          <td class="num"><b>{a['leads']}</b></td>
          <td class="num" style="font-weight:600;color:{cpl_color}">{cpl_val}</td>
        </tr>"""

    # Embudo HTML
    max_f = funnel[0][1] if funnel else 1
    funnel_html = ""
    for i, (label, val) in enumerate(funnel):
        w = val / max_f * 100
        if i > 0:
            prev = funnel[i-1][1]
            pct  = int(val / prev * 100) if prev else 0
            arrow = "↑" if val > prev else "↓"
            funnel_html += f'<div class="drop">{arrow} {pct}% vs. paso anterior</div>'
        text_color = "#fff" if w > 55 else "var(--ink)"
        funnel_html += f"""<div class="step">
          <div class="fill" style="width:{max(w,1.5):.1f}%"></div>
          <span class="lbl">{label}</span>
          <span class="val" style="color:{text_color}">{val}</span>
        </div>"""

    # Cohortes HTML
    cohortes_html = ""
    for label, total, resp, tasa in cohortes:
        is_last = (label == cohortes[-1][0]) if cohortes else False
        now_class = "now" if is_last else ""
        cohortes_html += f"""<div class="wk-c {now_class}">
          <div class="w-lab">{label}</div>
          <div class="wk-row"><span>Leads</span><span class="v">{total}</span></div>
          <div class="wk-row"><span>Contestaron</span><span class="v">{resp}</span></div>
          <div class="wk-row"><span>Tasa</span><span class="v">{tasa}</span></div>
        </div>"""

    # Barras placements
    def bars_html(items, fmt_val):
        if not items:
            return "<p style='color:var(--slate);font-size:13px'>Sin datos</p>"
        max_v = max(v for _, v in items) if items else 1
        out = ""
        for label, val in items:
            pct = val / max_v * 100
            out += f"""<div class="bar-row">
              <span>{label}</span>
              <span class="bar-track"><span class="bar-fill" style="width:{pct:.1f}%"></span></span>
              <span class="n">{fmt_val(val)}</span>
            </div>"""
        return out

    place_spend_html = bars_html(
        [(p["name"], p["spend"]) for p in placements],
        lambda v: f"${v:.0f}"
    )
    place_cpl_items = sorted(
        [(p["name"], p["cpl"]) for p in placements if p["cpl"]],
        key=lambda x: x[1]
    )
    max_cpl = max((v for _, v in place_cpl_items), default=1)
    place_cpl_html = ""
    for label, val in place_cpl_items:
        pct   = val / max_cpl * 100
        color = "var(--good)" if val < 2.0 else "var(--ink)"
        place_cpl_html += f"""<div class="bar-row">
          <span>{label}</span>
          <span class="bar-track"><span class="bar-fill" style="width:{pct:.1f}%;background:{color}"></span></span>
          <span class="n">${val:.2f}</span>
        </div>"""

    # Piezas × ventas
    piezas_rows = ""
    for pieza, d in piezas:
        conv = d["sep"] / d["total"] * 100 if d["total"] else 0
        piezas_rows += f"""<tr>
          <td>{pieza}</td>
          <td class="num">{d['total']}</td>
          <td class="num">{d['sep']}</td>
          <td class="num">{conv:.1f}%</td>
        </tr>"""

    # Público
    pub_html = bars_html(publicos, lambda v: str(v))

    # Distritos
    dist_html = bars_html(distritos, lambda v: str(v))

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{CLIENT_NAME} · {period}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap');
  :root{{
    --ink:#0F2038; --paper:#F3F1EC; --card:#FFFFFF;
    --amber:#E9A33A; --amber-dim:#F6E3BE; --slate:#63758C; --rule:#DCD8D0;
    --good:#2F7D5D; --bad:#B8453C;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--paper);color:var(--ink);font-family:'Archivo',system-ui,sans-serif;-webkit-font-smoothing:antialiased;padding:28px 22px 60px}}
  .wrap{{max-width:1180px;margin:0 auto}}
  header{{border-bottom:2px solid var(--ink);padding-bottom:18px;margin-bottom:26px}}
  .eyebrow{{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--slate)}}
  h1{{font-size:clamp(28px,4.6vw,46px);font-weight:800;letter-spacing:-.03em;line-height:1;margin:8px 0 10px}}
  h1 em{{font-style:normal;background:var(--amber);padding:0 8px;border-radius:2px}}
  .meta-line{{font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--slate);display:flex;gap:18px;flex-wrap:wrap}}
  .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:1px;background:var(--rule);border:1px solid var(--rule);margin-bottom:34px}}
  .kpi{{background:var(--card);padding:18px 16px}}
  .kpi .k-label{{font-family:'IBM Plex Mono',monospace;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--slate);margin-bottom:10px}}
  .kpi .k-val{{font-size:32px;font-weight:800;letter-spacing:-.03em;line-height:1}}
  .kpi .k-sub{{font-size:12px;color:var(--slate);margin-top:6px}}
  .kpi.hero{{background:var(--ink);color:#fff}}
  .kpi.hero .k-label{{color:var(--amber)}}
  .kpi.hero .k-sub{{color:#A9B6C7}}
  section{{margin-bottom:38px}}
  .s-head{{display:flex;align-items:baseline;gap:12px;border-bottom:1px solid var(--ink);padding-bottom:8px;margin-bottom:18px}}
  .s-head h2{{font-size:17px;font-weight:600;letter-spacing:-.01em}}
  .s-head .note{{font-size:12px;color:var(--slate);margin-left:auto;text-align:right}}
  .card{{background:var(--card);border:1px solid var(--rule);padding:20px}}
  .grid-b{{display:grid;grid-template-columns:1.35fr 1fr;gap:18px}}
  @media(max-width:860px){{.grid-b{{grid-template-columns:1fr}}}}
  .legend{{display:flex;gap:16px;font-size:11.5px;color:var(--slate);margin-top:14px;flex-wrap:wrap}}
  .legend i{{width:11px;height:11px;display:inline-block;margin-right:6px;vertical-align:-1px;border-radius:1px}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th{{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--slate);text-align:right;padding:0 10px 9px;border-bottom:1px solid var(--ink);white-space:nowrap}}
  th:first-child{{text-align:left}}
  td{{padding:11px 10px;border-bottom:1px solid var(--rule);text-align:right;font-variant-numeric:tabular-nums}}
  td:first-child{{text-align:left;font-weight:500}}
  tbody tr:hover{{background:#FBF9F5}}
  .num{{font-family:'IBM Plex Mono',monospace}}
  .win{{background:var(--amber-dim)!important}}
  .callout{{background:var(--ink);color:#fff;padding:22px 24px;border-left:5px solid var(--amber)}}
  .callout h3{{font-size:14px;font-weight:600;color:var(--amber);margin-bottom:10px}}
  .callout ul{{list-style:none;display:grid;gap:9px}}
  .callout li{{font-size:13.5px;line-height:1.5;color:#DDE4EC;padding-left:16px;position:relative}}
  .callout li::before{{content:'';position:absolute;left:0;top:8px;width:5px;height:5px;background:var(--amber)}}
  .callout b{{color:#fff;font-weight:600}}
  .wk{{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:1px;background:var(--rule);border:1px solid var(--rule)}}
  .wk-c{{background:var(--card);padding:14px}}
  .wk-c .w-lab{{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--slate);margin-bottom:10px}}
  .wk-row{{display:flex;justify-content:space-between;align-items:baseline;padding:4px 0;font-size:12px}}
  .wk-row .v{{font-family:'IBM Plex Mono',monospace;font-weight:600}}
  .wk-c.now{{box-shadow:inset 0 -3px 0 var(--amber)}}
  .fun{{display:flex;flex-direction:column;gap:7px}}
  .step{{position:relative;background:#EFEDE7;height:46px;display:flex;align-items:center;overflow:hidden}}
  .step .fill{{position:absolute;inset:0 auto 0 0;background:var(--ink);transition:width .8s cubic-bezier(.2,.8,.2,1)}}
  .step .lbl{{position:relative;z-index:2;padding-left:14px;font-size:12.5px;font-weight:500;color:#fff;mix-blend-mode:difference}}
  .step .val{{position:relative;z-index:2;margin-left:auto;padding-right:12px;font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600}}
  .drop{{font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:var(--slate);padding-left:14px;margin:-2px 0 1px}}
  .bars{{display:grid;gap:9px}}
  .bar-row{{display:grid;grid-template-columns:140px 1fr 52px;align-items:center;gap:10px;font-size:12.5px}}
  .bar-track{{height:16px;background:#EFEDE7}}
  .bar-fill{{height:100%;background:var(--ink)}}
  .bar-row .n{{font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:var(--slate);text-align:right}}
  svg{{display:block;width:100%;overflow:visible}}
  .ax{{font-family:'IBM Plex Mono',monospace;font-size:8.5px;fill:var(--slate)}}
  .gl{{stroke:var(--rule);stroke-width:1}}
  footer{{border-top:1px solid var(--rule);padding-top:14px;font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:var(--slate);line-height:1.7}}
  @media(prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="eyebrow">{CLIENT_NAME} · Cuenta {META_ACCOUNT} · Pauta + resultados comerciales</div>
  <h1>{CAMPAIGN_LABEL} <em>{period}</em></h1>
  <div class="meta-line">
    <span>{days} días desde el lanzamiento · campaña activa</span>
    <span>OUTCOME_LEADS · Meta Ads + CRM</span>
    <span>Moneda: {CURRENCY_META}</span>
    <span>Generado: {generated}</span>
  </div>
</header>

<!-- KPIs HERO -->
<div class="kpis">
  <div class="kpi hero">
    <div class="k-label">Clientes potenciales</div>
    <div class="k-val">{meta['leads']:.0f}</div>
    <div class="k-sub">{leads_day:.1f} leads por día</div>
  </div>
  <div class="kpi hero">
    <div class="k-label">Costo por cliente potencial</div>
    <div class="k-val">${meta['cpl']:.2f}</div>
    <div class="k-sub">≈ S/ {meta['cpl']*SOLES_RATE:.1f} por lead</div>
  </div>
  <div class="kpi">
    <div class="k-label">Inversión total</div>
    <div class="k-val">${meta['spend']:,.0f}</div>
    <div class="k-sub">≈ S/ {meta['spend']*SOLES_RATE:,.0f}</div>
  </div>
  <div class="kpi">
    <div class="k-label">Alcance</div>
    <div class="k-val">{meta['reach']/1000:.1f}k</div>
    <div class="k-sub">{meta['impressions']:,.0f} impresiones</div>
  </div>
  <div class="kpi">
    <div class="k-label">CTR</div>
    <div class="k-val">{meta['ctr']:.2f}%</div>
    <div class="k-sub">{meta['clicks']:,.0f} clics · CPC ${meta['cpc']:.2f}</div>
  </div>
  <div class="kpi">
    <div class="k-label">Visitas a caseta</div>
    <div class="k-val">{visit_count}</div>
    <div class="k-sub">${meta['spend']/visit_count:.0f} por visita</div>
  </div>
  <div class="kpi">
    <div class="k-label">Separaciones</div>
    <div class="k-val" style="color:var(--good)">{sep_count}</div>
    <div class="k-sub">{"${:.0f} por separación".format(cost_sep) if sep_count else "En proceso"}</div>
  </div>
</div>

<!-- EVOLUCIÓN DIARIA -->
<section>
  <div class="s-head">
    <h2>Evolución acumulada</h2>
    <div class="note">Leads diarios · CPL · inversión</div>
  </div>
  <div class="card">
    <svg id="svgDaily" viewBox="0 0 1100 320" role="img" aria-label="Leads diarios y CPL"></svg>
    <div class="legend">
      <span><i style="background:var(--ink)"></i>Leads</span>
      <span><i style="background:var(--amber)"></i>Costo por lead (USD)</span>
      <span><i style="background:#C9C4BA"></i>Inversión diaria</span>
    </div>
  </div>
</section>

<!-- ANUNCIOS -->
<section>
  <div class="s-head">
    <h2>Análisis por anuncio</h2>
    <div class="note">Ciclo completo · ordenado por inversión</div>
  </div>
  <div class="card" style="overflow-x:auto">
    <table>
      <thead>
        <tr><th>Anuncio</th><th>Inversión</th><th>Impr.</th><th>CTR</th><th>CPM</th><th>Leads</th><th>CPL</th></tr>
      </thead>
      <tbody>{ads_rows}</tbody>
    </table>
  </div>
</section>

<!-- CRUCE META × VENTAS -->
<section>
  <div class="s-head">
    <h2>Cruce Meta × Ventas</h2>
    <div class="note">Embudo comercial · Sheet actualizado al {generated}</div>
  </div>
  <div class="grid-b">
    <div class="card">
      <div class="fun">{funnel_html}</div>
    </div>
    <div class="callout">
      <h3>Lectura del embudo</h3>
      <ul>
        <li>Meta generó los leads a <b>${meta['cpl']:.2f} c/u</b>.</li>
        <li>De {next((v for k,v in funnel if 'ingresados' in k),0)} leads, <b>{next((v for k,v in funnel if 'Contestó' in k),0)} contestaron</b> el primer contacto.</li>
        <li><b>{visit_count} llegaron a la caseta</b> y <b>{sep_count} {'separó' if sep_count==1 else 'separaron'}</b>.</li>
        <li>La palanca con mayor retorno: <b>acelerar el primer contacto</b>. Hoy ~{100 - (next((v for k,v in funnel if 'Contactado'==k),0)*100//max(next((v for k,v in funnel if 'ingresados' in k),1),1))}% de los leads no reciben respuesta.</li>
      </ul>
    </div>
  </div>
</section>

<!-- PIEZAS × VENTAS -->
<section>
  <div class="s-head">
    <h2>Piezas por resultado comercial</h2>
    <div class="note">Leads ingresados vs. separaciones por anuncio</div>
  </div>
  <div class="card" style="overflow-x:auto">
    <table>
      <thead>
        <tr><th>Pieza / anuncio</th><th>Leads</th><th>Separaciones</th><th>Conv. %</th></tr>
      </thead>
      <tbody>{piezas_rows}</tbody>
    </table>
  </div>
</section>

<!-- TASA DE RESPUESTA Y PERFIL -->
<section>
  <div class="s-head">
    <h2>Tasa de respuesta y perfil</h2>
    <div class="note">Cohortes por mes · perfil de leads</div>
  </div>
  <div class="grid-b">
    <div class="card">
      <div class="wk" id="cohortes">{cohortes_html}</div>
    </div>
    <div class="card">
      <div class="eyebrow" style="margin-bottom:14px">Perfil del lead</div>
      <div class="bars">{pub_html}</div>
      <div style="margin-top:18px" class="eyebrow">Distrito de origen</div>
      <div class="bars" style="margin-top:12px">{dist_html}</div>
    </div>
  </div>
</section>

<!-- PLACEMENTS -->
<section>
  <div class="s-head">
    <h2>Ubicaciones</h2>
    <div class="note">Dónde se gastó vs. dónde rindió</div>
  </div>
  <div class="grid-b">
    <div class="card">
      <div class="eyebrow" style="margin-bottom:14px">Inversión por ubicación (USD)</div>
      <div class="bars">{place_spend_html}</div>
    </div>
    <div class="card">
      <div class="eyebrow" style="margin-bottom:14px">Costo por lead (USD) · menor es mejor</div>
      <div class="bars">{place_cpl_html}</div>
    </div>
  </div>
</section>

<footer>
  Fuente pauta: Meta Ads vía Windsor.ai · cuenta {META_ACCOUNT} · campaña {CAMPAIGN_ID} · rango {date_from.strftime('%d/%m/%Y')} – {today.strftime('%d/%m/%Y')}.<br>
  Fuente ventas: Google Sheets · Hoja 3 · corte al {generated}. Embudo gestionado manualmente por equipo comercial.<br>
  Publicidad con <b>Diego</b> · No es magia. Son métricas.
</footer>

</div>

<script>
/* ── gráfico diario SVG ── */
(function(){{
  const H_LABELS={js(h_labels)};
  const H_LEADS ={js(h_leads)};
  const H_CPL   ={js(h_cpl)};
  const H_SPEND ={js(h_spend)};

  const W=1100,H=320,ml=42,mr=50,mt=20,mb=50;
  const iw=W-ml-mr, ih=H-mt-mb;
  const n=H_LABELS.length;
  if(!n){{document.getElementById('svgDaily').innerHTML='<text x="550" y="160" text-anchor="middle" class="ax">Sin datos históricos aún</text>';return;}}

  const maxL=Math.max(...H_LEADS,1);
  const maxC=Math.max(...H_CPL.filter(v=>v>0),1)*1.15;
  const bw=iw/n;

  let s='';
  // grid
  for(let i=0;i<=5;i++){{
    const y=mt+ih-(ih*i/5);
    s+=`<line class="gl" x1="${{ml}}" y1="${{y}}" x2="${{ml+iw}}" y2="${{y}}"/>`;
    s+=`<text class="ax" x="${{ml-8}}" y="${{y+3}}" text-anchor="end">${{Math.round(maxL*i/5)}}</text>`;
    s+=`<text class="ax" x="${{ml+iw+8}}" y="${{y+3}}" fill="#B98A2E">$${{(maxC*i/5).toFixed(1)}}</text>`;
  }}
  // spend line
  let sp='';
  H_SPEND.forEach((v,i)=>{{const x=ml+bw*i+bw/2,y=mt+ih-(ih*(v/Math.max(...H_SPEND,1)));sp+=(i?'L':'M')+x+','+y;}});
  s+=`<path d="${{sp}}" fill="none" stroke="#C9C4BA" stroke-width="1.5" stroke-dasharray="3 3"/>`;
  // bars
  H_LEADS.forEach((v,i)=>{{
    const x=ml+bw*i+bw*0.2, w=bw*0.6, h=ih*(v/maxL), y=mt+ih-h;
    s+=`<rect x="${{x}}" y="${{y}}" width="${{w}}" height="${{Math.max(h,0)}}" fill="var(--ink)" rx="1"><title>${{H_LABELS[i]}}: ${{v}} leads</title></rect>`;
    if(i%Math.max(1,Math.floor(n/20))===0)
      s+=`<text class="ax" x="${{ml+bw*i+bw/2}}" y="${{mt+ih+14}}" text-anchor="middle">${{H_LABELS[i]}}</text>`;
  }});
  // CPL line
  let ln='',pts='';
  H_CPL.forEach((v,i)=>{{
    if(!v)return;
    const x=ml+bw*i+bw/2, y=mt+ih-(ih*Math.min(v,maxC)/maxC);
    ln+=(ln?'L':'M')+x+','+y;
    pts+=`<circle cx="${{x}}" cy="${{y}}" r="2.5" fill="var(--amber)" stroke="#fff" stroke-width="1"><title>${{H_LABELS[i]}}: $${{v}} CPL</title></circle>`;
  }});
  s+=`<path d="${{ln}}" fill="none" stroke="var(--amber)" stroke-width="2" stroke-linejoin="round"/>`+pts;
  document.getElementById('svgDaily').innerHTML=s;
}})();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    today     = dt.date.today()
    date_from = CAMPAIGN_START

    print(f"Generando dashboard {CLIENT_NAME}: {date_from} → {today}")

    # Datos Meta Ads
    print("Consultando Windsor.ai (Meta Ads)…")
    daily_meta, by_ad, by_place = fetch_meta(date_from, today)

    meta       = agg_meta(daily_meta)
    ads        = agg_ads(by_ad)
    placements = agg_placements(by_place)

    print(f"  → {meta['leads']:.0f} leads · ${meta['spend']:.2f} invertidos · CPL ${meta['cpl']:.2f}")

    # Datos Sheet
    print("Leyendo Google Sheet (embudo comercial)…")
    leads_sheet = fetch_sheet()
    print(f"  → {len(leads_sheet)} registros en Hoja 3")

    funnel    = agg_funnel(leads_sheet)
    piezas    = agg_by_pieza(leads_sheet)
    distritos = agg_distritos(leads_sheet)
    publicos  = agg_publico(leads_sheet)
    cohortes  = agg_cohortes_mes(leads_sheet)

    # Histórico
    hist = load_history()
    hist = save_history(hist, today, meta, funnel)

    # HTML
    print("Generando HTML…")
    html = build_html(
        date_from, today,
        meta, ads, placements,
        funnel, piezas, distritos, publicos, cohortes, hist
    )
    os.makedirs(os.path.dirname(OUTPUT_PATH) if os.path.dirname(OUTPUT_PATH) else ".", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"OK → {OUTPUT_PATH} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
