#!/usr/bin/env python3
"""
Relatório Semanal de Horas — LSC Arquitetura
Toda sexta às 18h (BRT): agrega horas e custos por projeto/pessoa
(semana atual + acumulado total) e envia e-mail HTML.
"""

import os
import smtplib
import requests
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_DESTINO      = os.environ["EMAIL_DESTINO"]

HORAS_DB_ID = "137fab6becce8004bbcde641510baffd"

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

MESES_ABREV = ["jan","fev","mar","abr","mai","jun",
               "jul","ago","set","out","nov","dez"]

STATUS_INATIVO = {"finalizado","cancelado","arquivado","concluído","concluido","suspenso"}

PESSOAS = ["Lícia", "Willian"]

_proj_cache: dict = {}


# ── Datas ───────────────────────────────────────────────────────────────────────

def semana_atual():
    hoje = date.today()
    seg  = hoje - timedelta(days=hoje.weekday())
    sex  = seg + timedelta(days=4)
    return seg, sex


# ── Formatação ──────────────────────────────────────────────────────────────────

def fmt_h(minutos):
    m = int(minutos or 0)
    if m <= 0:
        return "—"
    h, r = divmod(m, 60)
    return f"{h}h {r:02d}min" if r else f"{h}h"


def fmt_r(valor):
    v = float(valor or 0)
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


# ── API Notion ──────────────────────────────────────────────────────────────────

def api_post(path, body):
    r = requests.post(f"https://api.notion.com/v1/{path}", headers=HEADERS, json=body)
    r.raise_for_status()
    return r.json()


def api_get(path):
    r = requests.get(f"https://api.notion.com/v1/{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def buscar_todos_registros():
    rows, payload = [], {"page_size": 100}
    while True:
        data = api_post(f"databases/{HORAS_DB_ID}/query", payload)
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return rows


def info_projeto(pid):
    if not pid:
        return None, None
    key = pid.replace("-", "")
    if key in _proj_cache:
        c = _proj_cache[key]
        return c["nome"], c["status"]
    try:
        page  = api_get(f"pages/{pid}")
        props = page.get("properties", {})
        nome = status = None
        for pv in props.values():
            if pv.get("type") == "title":
                nome = "".join(x["plain_text"] for x in pv.get("title", []))
            if pv.get("type") == "status" and pv.get("status"):
                status = pv["status"].get("name", "")
        _proj_cache[key] = {"nome": nome, "status": status}
    except Exception:
        _proj_cache[key] = {"nome": None, "status": None}
    return _proj_cache[key]["nome"], _proj_cache[key]["status"]


# ── Extração de campos ──────────────────────────────────────────────────────────

def _formula_num(v):
    """
    Lê campo fórmula do Notion que pode retornar:
      - type "number"  → float direto
      - type "string"  → "R$ 1.234,56" ou "150,5" → float parseado
    """
    if v.get("type") != "formula":
        return 0.0
    f = v["formula"]
    if f.get("type") == "number":
        n = f.get("number")
        return float(n) if n is not None else 0.0
    if f.get("type") == "string":
        s = (f.get("string") or "").replace("R$", "").replace(" ", "").strip()
        # Formato BR: pontos são separadores de milhar, vírgula é decimal
        # Ex: "1.234,56" → remove pontos → "1234,56" → troca vírgula → "1234.56"
        s = s.replace(".", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return 0.0
    return 0.0


def extrair(r):
    props = r.get("properties", {})

    def formula_num(campo):
        return max(0.0, _formula_num(props.get(campo, {})))

    def data_inicio():
        v = props.get("Inicio", {})
        if v.get("type") == "date" and v.get("date"):
            s = v["date"].get("start", "")
            if s:
                try:
                    return date.fromisoformat(s[:10])
                except ValueError:
                    pass
        return None

    def criado_por():
        v = props.get("Criado por", {})
        if v.get("type") == "created_by":
            return v.get("created_by", {}).get("name", "") or ""
        return ""

    def projeto_id():
        v = props.get("2026 | Projetos", {})
        if v.get("type") == "relation":
            rels = v.get("relation", [])
            if rels:
                return rels[0].get("id")
        return None

    def status_projeto():
        v = props.get("Status do Projeto", {})
        if v.get("type") == "rollup":
            for item in v["rollup"].get("array", []):
                if item.get("type") == "status" and item.get("status"):
                    return item["status"].get("name", "")
        return None

    return {
        "inicio":         data_inicio(),
        "criado_por":     criado_por(),
        "projeto_id":     projeto_id(),
        "minutos":        formula_num("Minutos"),
        "custo":          formula_num("Custo"),
        "status_projeto": status_projeto(),
    }


# ── Lógica de pessoa / status ────────────────────────────────────────────────────

def pessoa_key(nome):
    n = (nome or "").lower()
    if "lícia" in n or "licia" in n:
        return "Lícia"
    if "willian" in n:
        return "Willian"
    return nome or "Outro"


def eh_inativo(status):
    if not status:
        return False
    return any(s in status.lower() for s in STATUS_INATIVO)


# ── Agregação ───────────────────────────────────────────────────────────────────

def _z():
    return {"min": 0.0, "custo": 0.0}


def agregar(registros, seg, sex):
    bruto: dict = {}

    for reg in registros:
        pid = reg["projeto_id"]
        if not pid or eh_inativo(reg["status_projeto"]):
            continue

        pessoa = pessoa_key(reg["criado_por"])

        if pid not in bruto:
            bruto[pid] = {"semana": {}, "total": {}}
        p = bruto[pid]

        if pessoa not in p["total"]:
            p["total"][pessoa] = _z()
        p["total"][pessoa]["min"]   += reg["minutos"]
        p["total"][pessoa]["custo"] += reg["custo"]

        ini = reg["inicio"]
        if ini and seg <= ini <= sex:
            if pessoa not in p["semana"]:
                p["semana"][pessoa] = _z()
            p["semana"][pessoa]["min"]   += reg["minutos"]
            p["semana"][pessoa]["custo"] += reg["custo"]

    projetos = {}
    for pid, dados in bruto.items():
        nome, status = info_projeto(pid)
        if eh_inativo(status):
            continue
        dados["nome"] = nome or pid
        projetos[pid] = dados

    return projetos


# ── HTML helpers ─────────────────────────────────────────────────────────────────

_TD  = "padding:10px 12px;font-size:13px;vertical-align:middle;border-bottom:1px solid #f8f8f8;"
_TH  = ("text-align:{align};font-size:10px;text-transform:uppercase;letter-spacing:.5px;"
        "color:#aaa;padding:8px 12px;border-bottom:1px solid #f0f0f0;font-weight:500;")


def th(txt, align="right"):
    return f'<th style="{_TH.format(align=align)}">{txt}</th>'


def td(txt, bold=False, align="left", cor=None, bg=None):
    st = _TD + f"text-align:{align};"
    if bold: st += "font-weight:600;"
    if cor:  st += f"color:{cor};"
    if bg:   st += f"background:{bg};"
    return f'<td style="{st}">{txt}</td>'


def table_wrap(thead, tbody):
    return (
        '<div style="border:1px solid #eee;border-radius:8px;overflow:hidden;margin-bottom:10px;">'
        '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
        f'<thead><tr>{thead}</tr></thead>'
        f'<tbody>{tbody}</tbody>'
        '</table></div>'
    )


def sec_title(txt):
    return (
        f'<p style="margin:0 0 14px;font-size:11px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:1.5px;color:#888;padding-bottom:8px;border-bottom:1px solid #f0f0f0;">{txt}</p>'
    )


def proj_title(nome):
    return (
        f'<p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#1a1a1a;">{nome}</p>'
    )


def custo_lines(cs, ct):
    def row(label, d):
        l = fmt_r(d.get("Lícia", 0))
        w = fmt_r(d.get("Willian", 0))
        t = fmt_r(sum(d.values()))
        return (
            f'<p style="margin:3px 0;font-size:12px;color:#555;">'
            f'<span style="color:#bbb;display:inline-block;width:90px;">{label}</span>'
            f'Lícia&nbsp;{l}&nbsp; &middot; &nbsp;Willian&nbsp;{w}&nbsp; &middot; &nbsp;Total&nbsp;{t}'
            f'</p>'
        )
    return row("Custo semana", cs) + row("Custo total", ct)


def divider():
    return '<hr style="border:none;border-top:1px solid #f0f0f0;margin:26px 0;">'


# ── Cards de resumo ──────────────────────────────────────────────────────────────

def card(label, valor, cor_label, cor_valor, bg):
    return (
        f'<div style="flex:1;min-width:130px;background:{bg};border-radius:8px;padding:14px 16px;">'
        f'<p style="margin:0;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:{cor_label};">{label}</p>'
        f'<p style="margin:6px 0 0;font-size:20px;font-weight:700;color:{cor_valor};">{valor}</p>'
        f'</div>'
    )


# ── Geração do HTML ──────────────────────────────────────────────────────────────

def gerar_html(projetos, seg, sex):
    data_ini = f"{seg.day:02d}"
    data_fim = f"{sex.day:02d}/{MESES_ABREV[sex.month - 1]}/{sex.year}"
    periodo  = f"{data_ini} a {data_fim}"

    # Acumuladores consolidados
    esc_sem = {p: _z() for p in PESSOAS}
    esc_tot = {p: _z() for p in PESSOAS}
    for proj in projetos.values():
        for p in PESSOAS:
            esc_sem[p]["min"]   += proj["semana"].get(p, _z())["min"]
            esc_sem[p]["custo"] += proj["semana"].get(p, _z())["custo"]
            esc_tot[p]["min"]   += proj["total"].get(p,  _z())["min"]
            esc_tot[p]["custo"] += proj["total"].get(p,  _z())["custo"]

    sem_min   = sum(v["min"]   for v in esc_sem.values())
    sem_custo = sum(v["custo"] for v in esc_sem.values())
    tot_min   = sum(v["min"]   for v in esc_tot.values())
    tot_custo = sum(v["custo"] for v in esc_tot.values())

    n_total = len(projetos)
    n_semana = sum(
        1 for p in projetos.values()
        if any(p["semana"].get(pe, _z())["min"] > 0 for pe in PESSOAS)
    )

    # Cards
    cards_html = (
        '<div style="display:flex;gap:10px;margin-bottom:28px;flex-wrap:wrap;">'
        + card("Horas na semana", fmt_h(sem_min),   "#2c5282", "#2c5282", "#ebf4ff")
        + card("Custo semana",    fmt_r(sem_custo),  "#276749", "#276749", "#f0fff4")
        + card("Horas acumuladas",fmt_h(tot_min),   "#92400e", "#92400e", "#fffbeb")
        + card("Projetos ativos", f"{n_semana} / {n_total}", "#555", "#1a1a1a", "#f7f7f7")
        + '</div>'
    )

    # Projetos: primeiro os que têm horas na semana, depois o resto
    proj_ordenados = sorted(
        projetos.items(),
        key=lambda x: (
            -sum(x[1]["semana"].get(p, _z())["min"] for p in PESSOAS),
            x[1]["nome"],
        ),
    )

    thead = th("", align="left") + th("Semana") + th("Total")
    projetos_html = ""

    for _, proj in proj_ordenados:
        tbody  = ""
        p_sem  = p_tot = 0.0

        for pessoa in PESSOAS:
            sem = proj["semana"].get(pessoa, _z())["min"]
            tot = proj["total"].get(pessoa,  _z())["min"]
            p_sem += sem
            p_tot += tot
            tbody += (
                f'<tr>'
                + td(pessoa)
                + td(fmt_h(sem), align="right", cor="#1a1a1a")
                + td(fmt_h(tot), align="right", cor="#666")
                + '</tr>'
            )

        tbody += (
            f'<tr>'
            + td("Total", bold=True, bg="#fafafa")
            + td(fmt_h(p_sem), bold=True, align="right", bg="#fafafa")
            + td(fmt_h(p_tot), bold=True, align="right", cor="#666", bg="#fafafa")
            + '</tr>'
        )

        cs = {p: proj["semana"].get(p, _z())["custo"] for p in PESSOAS}
        ct = {p: proj["total"].get(p,  _z())["custo"] for p in PESSOAS}

        tem_semana = p_sem > 0
        wrapper_st = "" if tem_semana else "opacity:.75;"

        projetos_html += (
            f'<div style="margin-bottom:22px;{wrapper_st}">'
            + proj_title(proj["nome"])
            + table_wrap(thead, tbody)
            + custo_lines(cs, ct)
            + '</div>'
        )

    # Consolidado
    tbody_consol = ""
    for pessoa in PESSOAS:
        tbody_consol += (
            f'<tr>'
            + td(pessoa)
            + td(fmt_h(esc_sem[pessoa]["min"]), align="right", cor="#1a1a1a")
            + td(fmt_h(esc_tot[pessoa]["min"]), align="right", cor="#666")
            + '</tr>'
        )
    tbody_consol += (
        f'<tr>'
        + td("Total", bold=True, bg="#fafafa")
        + td(fmt_h(sem_min), bold=True, align="right", bg="#fafafa")
        + td(fmt_h(tot_min), bold=True, align="right", cor="#666", bg="#fafafa")
        + '</tr>'
    )
    cs_esc = {p: esc_sem[p]["custo"] for p in PESSOAS}
    ct_esc = {p: esc_tot[p]["custo"] for p in PESSOAS}

    consol_html = (
        sec_title("Consolidado do Escritório")
        + table_wrap(thead, tbody_consol)
        + custo_lines(cs_esc, ct_esc)
    )

    corpo = (
        cards_html
        + sec_title(f"Projetos — {n_semana} com horas nesta semana")
        + projetos_html
        + divider()
        + consol_html
        + '<p style="text-align:right;font-size:11px;color:#ccc;margin:24px 0 0;">'
          'Gerado automaticamente · LSC Arquitetura</p>'
    )

    return (
        '<!DOCTYPE html><html>'
        '<head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#f0f0f0;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;color:#1a1a1a;">'
        '<div style="max-width:720px;margin:32px auto;">'

        # Header escuro
        '<div style="background:#1a1a1a;padding:24px 28px;border-radius:10px 10px 0 0;">'
        '<p style="margin:0;color:#666;font-size:11px;text-transform:uppercase;'
        'letter-spacing:1.5px;">Relatório Semanal · LSC Arquitetura</p>'
        f'<p style="margin:6px 0 0;color:#fff;font-size:20px;font-weight:600;">{periodo}</p>'
        f'<p style="margin:4px 0 0;color:#888;font-size:12px;">'
        f'{n_total} projetos · Gerado automaticamente</p>'
        '</div>'

        # Corpo branco
        f'<div style="background:#fff;padding:28px;border-radius:0 0 10px 10px;">{corpo}</div>'
        '</div></body></html>'
    )


# ── Texto plano (fallback) ───────────────────────────────────────────────────────

def gerar_texto(projetos, seg, sex):
    linhas = []
    sep    = "━" * 38
    linhas.append(f"RELATÓRIO SEMANAL — {seg.day:02d} a {sex.day:02d}/{MESES_ABREV[sex.month-1]}/{sex.year}")

    esc_sem = {p: _z() for p in PESSOAS}
    esc_tot = {p: _z() for p in PESSOAS}

    for proj in sorted(projetos.values(), key=lambda x: x["nome"]):
        linhas += ["", sep, f"PROJETO | {proj['nome']}", sep, ""]
        linhas.append(f"{'':15}{'SEMANA':>12}{'TOTAL':>14}")
        s_min = t_min = 0.0

        for pessoa in PESSOAS:
            s = proj["semana"].get(pessoa, _z())["min"]
            t = proj["total"].get(pessoa,  _z())["min"]
            s_min += s; t_min += t
            linhas.append(f"{pessoa:<15}{fmt_h(s):>12}{fmt_h(t):>14}")
            esc_sem[pessoa]["min"]   += s
            esc_sem[pessoa]["custo"] += proj["semana"].get(pessoa, _z())["custo"]
            esc_tot[pessoa]["min"]   += t
            esc_tot[pessoa]["custo"] += proj["total"].get(pessoa,  _z())["custo"]

        linhas.append(f"{'Total':<15}{fmt_h(s_min):>12}{fmt_h(t_min):>14}")
        linhas.append("")
        cs = {p: proj["semana"].get(p, _z())["custo"] for p in PESSOAS}
        ct = {p: proj["total"].get(p,  _z())["custo"] for p in PESSOAS}
        linhas.append(f"Custo semana:   Lícia {fmt_r(cs['Lícia'])}  |  Willian {fmt_r(cs['Willian'])}  |  Total {fmt_r(sum(cs.values()))}")
        linhas.append(f"Custo total:    Lícia {fmt_r(ct['Lícia'])}  |  Willian {fmt_r(ct['Willian'])}  |  Total {fmt_r(sum(ct.values()))}")

    sem_min = sum(esc_sem[p]["min"]   for p in PESSOAS)
    tot_min = sum(esc_tot[p]["min"]   for p in PESSOAS)
    sem_cst = sum(esc_sem[p]["custo"] for p in PESSOAS)
    tot_cst = sum(esc_tot[p]["custo"] for p in PESSOAS)

    linhas += ["", sep, "CONSOLIDADO DO ESCRITÓRIO", sep]
    linhas.append(f"Total semana:   Lícia {fmt_h(esc_sem['Lícia']['min'])}  |  Willian {fmt_h(esc_sem['Willian']['min'])}  |  Total {fmt_h(sem_min)}")
    linhas.append(f"Custo semana:   Lícia {fmt_r(esc_sem['Lícia']['custo'])}  |  Willian {fmt_r(esc_sem['Willian']['custo'])}  |  Total {fmt_r(sem_cst)}")
    linhas.append("")
    linhas.append(f"Total geral:    Lícia {fmt_h(esc_tot['Lícia']['min'])}  |  Willian {fmt_h(esc_tot['Willian']['min'])}  |  Total {fmt_h(tot_min)}")
    linhas.append(f"Custo geral:    Lícia {fmt_r(esc_tot['Lícia']['custo'])}  |  Willian {fmt_r(esc_tot['Willian']['custo'])}  |  Total {fmt_r(tot_cst)}")
    linhas.append("")
    return "\n".join(linhas)


# ── Envio ────────────────────────────────────────────────────────────────────────

def enviar(assunto, html, texto):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_DESTINO
    msg.attach(MIMEText(texto, "plain", "utf-8"))
    msg.attach(MIMEText(html,  "html",  "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.sendmail(GMAIL_USER, EMAIL_DESTINO, msg.as_string())


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    seg, sex = semana_atual()
    print(f"Semana: {seg} a {sex}")

    print("Buscando registros de horas...")
    raw = buscar_todos_registros()
    print(f"{len(raw)} registros encontrados.")

    print("Extraindo campos...")
    registros = [extrair(r) for r in raw]

    print("Agregando por projeto/pessoa...")
    projetos = agregar(registros, seg, sex)
    print(f"{len(projetos)} projetos com registros.")

    data_ini = f"{seg.day:02d}"
    data_fim = f"{sex.day:02d}/{MESES_ABREV[sex.month - 1]}/{sex.year}"
    assunto  = f"Relatório Semanal | {data_ini} a {data_fim}"

    html  = gerar_html(projetos, seg, sex)
    texto = gerar_texto(projetos, seg, sex)
    print(texto)

    print("Enviando e-mail...")
    enviar(assunto, html, texto)
    print("✓ Relatório semanal enviado.")


if __name__ == "__main__":
    main()
