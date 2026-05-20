#!/usr/bin/env python3
"""
Relatório Semanal de Horas — LSC Arquitetura
Toda sexta às 18h (BRT).

Seção 1 — Horas na Semana: por pessoa (Lícia / Willian), lista de projetos
           com tempo e custo desta semana.
Seção 2 — Custo por Projeto: todos projetos ativos, tempo e custo acumulados
           por Lícia e Willian.
"""

import json
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


# ── Leitura de campos ────────────────────────────────────────────────────────────

def _ler_formula(v):
    """
    Lê campo fórmula. Notion pode retornar:
      type "number"  → float direto
      type "string"  → "R$ 1.234,56" ou "1.234,56" → parse BR
      type "boolean" / null → 0
    """
    if v.get("type") != "formula":
        return 0.0
    f = v.get("formula", {})
    if f.get("type") == "number":
        n = f.get("number")
        return float(n) if n is not None else 0.0
    if f.get("type") == "string":
        # "R$ 1.234,56" → remove símbolo e espaços → "1.234,56"
        # ponto = milhar, vírgula = decimal (formato BR)
        s = (f.get("string") or "").replace("R$", "").replace(" ", "")
        s = s.replace(".", "").replace(",", ".")   # "1234.56"
        try:
            return float(s)
        except ValueError:
            return 0.0
    return 0.0


def _ler_rollup_number(v):
    """Lê rollup do tipo number (aggregation: sum)."""
    if v.get("type") != "rollup":
        return 0.0
    ro = v.get("rollup", {})
    if ro.get("type") == "number":
        n = ro.get("number")
        return float(n) if n is not None else 0.0
    return 0.0


def debug_custo(raw_registros):
    """Imprime campos de custo dos primeiros 5 registros com projeto — visível no log do Actions."""
    count = 0
    for r in raw_registros:
        props = r.get("properties", {})
        if not props.get("2026 | Projetos", {}).get("relation"):
            continue
        if count >= 5:
            break
        count += 1
        print(f"\n=== DEBUG CUSTO — registro {count} ===")
        for campo in ["Minutos", "Custo", "V.H Escritório", "V.H. Equipe"]:
            raw = props.get(campo, "NÃO ENCONTRADO")
            print(f"  {campo}: {json.dumps(raw, ensure_ascii=False, default=str)[:300]}")


def extrair(r):
    props = r.get("properties", {})

    minutos = max(0.0, _ler_formula(props.get("Minutos", {})))

    # Custo: tenta fórmula; se zero, tenta calcular via rollups V.H.
    custo = max(0.0, _ler_formula(props.get("Custo", {})))
    if custo == 0.0 and minutos > 0:
        vh = (_ler_rollup_number(props.get("V.H Escritório", {}))
              + _ler_rollup_number(props.get("V.H. Equipe", {})))
        if vh > 0:
            custo = (minutos / 60.0) * vh

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
        "minutos":        minutos,
        "custo":          custo,
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


def eh_ativo_secao2(status):
    """
    Seção 2 exibe projetos em criação ou detalhamento.
    Se o status não for reconhecido inclui por precaução.
    """
    if not status:
        return True
    if eh_inativo(status):
        return False
    s = status.lower()
    return any(k in s for k in ("criação", "criacao", "detalhamento", "andamento", "ativo"))


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

        # acumulado total
        if pessoa not in p["total"]:
            p["total"][pessoa] = _z()
        p["total"][pessoa]["min"]   += reg["minutos"]
        p["total"][pessoa]["custo"] += reg["custo"]

        # semana corrente
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
        dados["nome"]           = nome or pid
        dados["status_projeto"] = status
        projetos[pid] = dados

    return projetos


# ── HTML helpers ─────────────────────────────────────────────────────────────────

_TD = "padding:10px 12px;font-size:13px;vertical-align:middle;border-bottom:1px solid #f8f8f8;"
_TH = ("text-align:{align};font-size:10px;text-transform:uppercase;letter-spacing:.5px;"
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
        '<div style="border:1px solid #eee;border-radius:8px;overflow:hidden;margin-bottom:14px;">'
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


def sub_title(txt, n=None):
    cnt = (
        f'<span style="background:#f0f0f0;color:#888;font-size:10px;padding:1px 6px;'
        f'border-radius:99px;font-weight:500;margin-left:6px;">{n}</span>'
    ) if n is not None else ""
    return f'<p style="margin:0 0 10px;font-size:12px;font-weight:600;color:#555;">{txt}{cnt}</p>'


def proj_label(nome):
    return (
        f'<p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#1a1a1a;">{nome}</p>'
    )


def divider():
    return '<hr style="border:none;border-top:1px solid #f0f0f0;margin:26px 0;">'


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

    # ── totais consolidados para os cards ──────────────────────────────────────
    sem_min = sem_cst = tot_min = 0.0
    for proj in projetos.values():
        for p in PESSOAS:
            sem_min += proj["semana"].get(p, _z())["min"]
            sem_cst += proj["semana"].get(p, _z())["custo"]
            tot_min += proj["total"].get(p,  _z())["min"]

    n_total  = len(projetos)
    n_semana = sum(
        1 for p in projetos.values()
        if any(p["semana"].get(pe, _z())["min"] > 0 for pe in PESSOAS)
    )

    cards_html = (
        '<div style="display:flex;gap:10px;margin-bottom:28px;flex-wrap:wrap;">'
        + card("Horas na semana",  fmt_h(sem_min), "#2c5282", "#2c5282", "#ebf4ff")
        + card("Custo semana",     fmt_r(sem_cst), "#276749", "#276749", "#f0fff4")
        + card("Horas acumuladas", fmt_h(tot_min), "#92400e", "#92400e", "#fffbeb")
        + card("Projetos ativos",  f"{n_semana} / {n_total}", "#555", "#1a1a1a", "#f7f7f7")
        + '</div>'
    )

    # ── Seção 1: Horas na Semana ───────────────────────────────────────────────
    thead_sem = th("Projeto", align="left") + th("Tempo") + th("Custo semana")

    def pessoa_tabela_semana(pessoa):
        linhas = [
            (proj["nome"], proj["semana"].get(pessoa, _z()))
            for proj in projetos.values()
            if proj["semana"].get(pessoa, _z())["min"] > 0
        ]
        if not linhas:
            return (
                f'<p style="color:#bbb;font-size:13px;font-style:italic;'
                f'padding:6px 0;">Sem horas registradas nesta semana.</p>'
            )
        linhas.sort(key=lambda x: -x[1]["min"])
        tbody = ""
        t_min = t_cst = 0.0
        for nome, d in linhas:
            t_min += d["min"]
            t_cst += d["custo"]
            tbody += (
                "<tr>"
                + td(nome)
                + td(fmt_h(d["min"]), align="right", cor="#1a1a1a")
                + td(fmt_r(d["custo"]), align="right", cor="#276749")
                + "</tr>"
            )
        tbody += (
            "<tr>"
            + td("Total", bold=True, bg="#fafafa")
            + td(fmt_h(t_min), bold=True, align="right", bg="#fafafa")
            + td(fmt_r(t_cst), bold=True, align="right", cor="#276749", bg="#fafafa")
            + "</tr>"
        )
        return table_wrap(thead_sem, tbody)

    s1_licia   = sub_title("Lícia")   + pessoa_tabela_semana("Lícia")
    s1_willian = sub_title("Willian") + pessoa_tabela_semana("Willian")

    s1 = (
        '<div style="margin-bottom:26px;">'
        + sec_title("Seção 1 — Horas na Semana")
        + s1_licia
        + s1_willian
        + '</div>'
    )

    # ── Seção 2: Custo por Projeto ─────────────────────────────────────────────
    thead_proj = (
        th("", align="left")
        + th("Tempo total")
        + th("Custo total")
    )

    proj_s2 = {
        pid: p for pid, p in projetos.items()
        if eh_ativo_secao2(p.get("status_projeto"))
    }
    # fallback: se nenhum passa no filtro, mostra todos ativos
    if not proj_s2:
        proj_s2 = projetos

    proj_s2_ordenados = sorted(proj_s2.items(), key=lambda x: x[1]["nome"])

    projetos_html = ""
    for _, proj in proj_s2_ordenados:
        tbody = ""
        t_min = t_cst = 0.0
        for pessoa in PESSOAS:
            d = proj["total"].get(pessoa, _z())
            t_min += d["min"]
            t_cst += d["custo"]
            tbody += (
                "<tr>"
                + td(pessoa)
                + td(fmt_h(d["min"]), align="right", cor="#1a1a1a")
                + td(fmt_r(d["custo"]), align="right", cor="#276749")
                + "</tr>"
            )
        tbody += (
            "<tr>"
            + td("Total", bold=True, bg="#fafafa")
            + td(fmt_h(t_min), bold=True, align="right", bg="#fafafa")
            + td(fmt_r(t_cst), bold=True, align="right", cor="#276749", bg="#fafafa")
            + "</tr>"
        )
        projetos_html += (
            '<div style="margin-bottom:20px;">'
            + proj_label(proj["nome"])
            + table_wrap(thead_proj, tbody)
            + '</div>'
        )

    s2 = (
        '<div style="margin-bottom:26px;">'
        + sec_title(f"Seção 2 — Custo por Projeto ({len(proj_s2)} projetos ativos)")
        + projetos_html
        + '</div>'
    )

    corpo = cards_html + s1 + divider() + s2 + (
        '<p style="text-align:right;font-size:11px;color:#ccc;margin:24px 0 0;">'
        'Gerado automaticamente · LSC Arquitetura</p>'
    )

    return (
        '<!DOCTYPE html><html>'
        '<head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#f0f0f0;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;color:#1a1a1a;">'
        '<div style="max-width:720px;margin:32px auto;">'
        '<div style="background:#1a1a1a;padding:24px 28px;border-radius:10px 10px 0 0;">'
        '<p style="margin:0;color:#666;font-size:11px;text-transform:uppercase;'
        'letter-spacing:1.5px;">Relatório Semanal · LSC Arquitetura</p>'
        f'<p style="margin:6px 0 0;color:#fff;font-size:20px;font-weight:600;">{periodo}</p>'
        f'<p style="margin:4px 0 0;color:#888;font-size:12px;">'
        f'{n_total} projetos · Gerado automaticamente</p>'
        '</div>'
        f'<div style="background:#fff;padding:28px;border-radius:0 0 10px 10px;">{corpo}</div>'
        '</div></body></html>'
    )


# ── Texto plano (fallback) ───────────────────────────────────────────────────────

def gerar_texto(projetos, seg, sex):
    sep    = "━" * 38
    linhas = [f"RELATÓRIO SEMANAL — {seg.day:02d} a {sex.day:02d}/{MESES_ABREV[sex.month-1]}/{sex.year}"]

    linhas += ["", sep, "SEÇÃO 1 — HORAS NA SEMANA", sep]
    for pessoa in PESSOAS:
        linhas += ["", f"  {pessoa.upper()}"]
        itens = [
            (proj["nome"], proj["semana"].get(pessoa, _z()))
            for proj in projetos.values()
            if proj["semana"].get(pessoa, _z())["min"] > 0
        ]
        itens.sort(key=lambda x: -x[1]["min"])
        if not itens:
            linhas.append("  Sem horas nesta semana.")
            continue
        t_min = t_cst = 0.0
        for nome, d in itens:
            t_min += d["min"]; t_cst += d["custo"]
            linhas.append(f"  {nome:<35}  {fmt_h(d['min']):>10}  {fmt_r(d['custo']):>12}")
        linhas.append(f"  {'Total':<35}  {fmt_h(t_min):>10}  {fmt_r(t_cst):>12}")

    linhas += ["", sep, "SEÇÃO 2 — CUSTO POR PROJETO", sep]
    proj_s2 = {pid: p for pid, p in projetos.items() if eh_ativo_secao2(p.get("status_projeto"))}
    if not proj_s2:
        proj_s2 = projetos
    for proj in sorted(proj_s2.values(), key=lambda x: x["nome"]):
        linhas += ["", f"  {proj['nome']}"]
        t_min = t_cst = 0.0
        for pessoa in PESSOAS:
            d = proj["total"].get(pessoa, _z())
            t_min += d["min"]; t_cst += d["custo"]
            linhas.append(f"    {pessoa:<10}  {fmt_h(d['min']):>10}  {fmt_r(d['custo']):>12}")
        linhas.append(f"    {'Total':<10}  {fmt_h(t_min):>10}  {fmt_r(t_cst):>12}")

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

    # Debug custo — imprime no log do Actions para diagnóstico
    print("\n--- DEBUG CUSTO (primeiros 5 registros com projeto) ---")
    debug_custo(raw)
    print("--- FIM DEBUG ---\n")

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
