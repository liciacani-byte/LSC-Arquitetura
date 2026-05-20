#!/usr/bin/env python3
"""
Relatório Semanal de Horas — LSC Arquitetura
Toda sexta às 18h (BRT): busca o Controle de Horas, agrega por projeto/pessoa
(semana atual + acumulado total) e envia por e-mail.
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

MESES_ABREV = ["jan", "fev", "mar", "abr", "mai", "jun",
               "jul", "ago", "set", "out", "nov", "dez"]

STATUS_INATIVO = {"finalizado", "cancelado", "arquivado", "concluído", "concluido", "suspenso"}

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
    if m == 0:
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
    """Retorna (nome, status) do projeto; usa cache para evitar chamadas repetidas."""
    if not pid:
        return None, None
    key = pid.replace("-", "")
    if key in _proj_cache:
        cached = _proj_cache[key]
        return cached["nome"], cached["status"]
    try:
        page = api_get(f"pages/{pid}")
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

def extrair(r):
    props = r.get("properties", {})

    def formula_num(campo):
        v = props.get(campo, {})
        if v.get("type") == "formula":
            f = v["formula"]
            if f.get("type") == "number":
                return float(f.get("number") or 0)
        return 0.0

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
            ro = v.get("rollup", {})
            for item in ro.get("array", []):
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


# ── Lógica de pessoa ────────────────────────────────────────────────────────────

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

def _zeros():
    return {"min": 0.0, "custo": 0.0}


def agregar(registros, seg, sex):
    bruto: dict = {}

    for reg in registros:
        pid = reg["projeto_id"]
        if not pid:
            continue
        if eh_inativo(reg["status_projeto"]):
            continue

        pessoa = pessoa_key(reg["criado_por"])

        if pid not in bruto:
            bruto[pid] = {"semana": {}, "total": {}}

        p = bruto[pid]

        if pessoa not in p["total"]:
            p["total"][pessoa] = _zeros()
        p["total"][pessoa]["min"]   += reg["minutos"]
        p["total"][pessoa]["custo"] += reg["custo"]

        ini = reg["inicio"]
        if ini and seg <= ini <= sex:
            if pessoa not in p["semana"]:
                p["semana"][pessoa] = _zeros()
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


# ── Geração do relatório ────────────────────────────────────────────────────────

def gerar_relatorio(projetos, seg, sex):
    linhas = []
    sep    = "━" * 38

    data_ini = f"{seg.day:02d}"
    data_fim = f"{sex.day:02d}/{MESES_ABREV[sex.month - 1]}/{sex.year}"
    linhas.append(f"RELATÓRIO SEMANAL — {data_ini} a {data_fim}")

    esc_sem = {p: _zeros() for p in PESSOAS}
    esc_tot = {p: _zeros() for p in PESSOAS}

    for proj in sorted(projetos.values(), key=lambda x: x["nome"]):
        linhas += ["", sep, f"PROJETO | {proj['nome']}", sep, ""]

        linhas.append(f"{'':15}{'SEMANA':>12}{'TOTAL':>14}")

        sem_min_total = tot_min_total = 0.0

        for pessoa in PESSOAS:
            sem = proj["semana"].get(pessoa, _zeros())
            tot = proj["total"].get(pessoa, _zeros())
            sem_min_total += sem["min"]
            tot_min_total += tot["min"]
            linhas.append(f"{pessoa:<15}{fmt_h(sem['min']):>12}{fmt_h(tot['min']):>14}")

            esc_sem[pessoa]["min"]   += sem["min"]
            esc_sem[pessoa]["custo"] += sem["custo"]
            esc_tot[pessoa]["min"]   += tot["min"]
            esc_tot[pessoa]["custo"] += tot["custo"]

        linhas.append(f"{'Total':<15}{fmt_h(sem_min_total):>12}{fmt_h(tot_min_total):>14}")
        linhas.append("")

        cs = {p: proj["semana"].get(p, _zeros())["custo"] for p in PESSOAS}
        ct = {p: proj["total"].get(p,  _zeros())["custo"] for p in PESSOAS}

        linhas.append(
            f"Custo semana:   Lícia {fmt_r(cs['Lícia'])}  |  "
            f"Willian {fmt_r(cs['Willian'])}  |  Total {fmt_r(sum(cs.values()))}"
        )
        linhas.append(
            f"Custo total:    Lícia {fmt_r(ct['Lícia'])}  |  "
            f"Willian {fmt_r(ct['Willian'])}  |  Total {fmt_r(sum(ct.values()))}"
        )

    linhas += ["", sep, "CONSOLIDADO DO ESCRITÓRIO", sep]

    sem_min_total = sum(esc_sem[p]["min"]   for p in PESSOAS)
    tot_min_total = sum(esc_tot[p]["min"]   for p in PESSOAS)
    sem_cst_total = sum(esc_sem[p]["custo"] for p in PESSOAS)
    tot_cst_total = sum(esc_tot[p]["custo"] for p in PESSOAS)

    linhas.append(
        f"Total semana:   Lícia {fmt_h(esc_sem['Lícia']['min'])}  |  "
        f"Willian {fmt_h(esc_sem['Willian']['min'])}  |  Total {fmt_h(sem_min_total)}"
    )
    linhas.append(
        f"Custo semana:   Lícia {fmt_r(esc_sem['Lícia']['custo'])}  |  "
        f"Willian {fmt_r(esc_sem['Willian']['custo'])}  |  Total {fmt_r(sem_cst_total)}"
    )
    linhas.append("")
    linhas.append(
        f"Total geral:    Lícia {fmt_h(esc_tot['Lícia']['min'])}  |  "
        f"Willian {fmt_h(esc_tot['Willian']['min'])}  |  Total {fmt_h(tot_min_total)}"
    )
    linhas.append(
        f"Custo geral:    Lícia {fmt_r(esc_tot['Lícia']['custo'])}  |  "
        f"Willian {fmt_r(esc_tot['Willian']['custo'])}  |  Total {fmt_r(tot_cst_total)}"
    )
    linhas.append("")

    return "\n".join(linhas)


# ── Envio ───────────────────────────────────────────────────────────────────────

def enviar(assunto, texto):
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
        "<body style='margin:0;padding:0;background:#f0f0f0;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'>"
        "<div style='max-width:700px;margin:32px auto;background:#fff;"
        "border-radius:10px;overflow:hidden;border:1px solid #e5e7eb;'>"
        "<div style='background:#1a1a1a;padding:20px 28px;'>"
        "<p style='margin:0;color:#888;font-size:11px;text-transform:uppercase;"
        "letter-spacing:1.5px;'>Relatório Semanal · LSC Arquitetura</p>"
        f"<p style='margin:6px 0 0;color:#fff;font-size:18px;font-weight:600;'>{assunto}</p>"
        "</div>"
        "<div style='padding:28px;'>"
        f"<pre style='font-family:\"Courier New\",Courier,monospace;font-size:13px;"
        f"line-height:1.7;color:#1a1a1a;white-space:pre-wrap;margin:0;'>{texto}</pre>"
        "</div>"
        "<div style='padding:12px 28px;border-top:1px solid #f0f0f0;text-align:right;'>"
        "<p style='margin:0;font-size:11px;color:#bbb;'>Gerado automaticamente · LSC Arquitetura</p>"
        "</div></div></body></html>"
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_DESTINO
    msg.attach(MIMEText(texto, "plain",  "utf-8"))
    msg.attach(MIMEText(html,  "html",   "utf-8"))
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
    print(f"{len(projetos)} projetos ativos com registros.")

    texto = gerar_relatorio(projetos, seg, sex)
    print(texto)

    assunto = (
        f"Relatório Semanal | {seg.day:02d} a {sex.day:02d}"
        f"/{MESES_ABREV[sex.month - 1]}/{sex.year}"
    )
    print("Enviando e-mail...")
    enviar(assunto, texto)
    print("✓ Relatório semanal enviado.")


if __name__ == "__main__":
    main()
