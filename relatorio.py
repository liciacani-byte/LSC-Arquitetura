#!/usr/bin/env python3
"""
Relatório Operacional do Escritório — Simplific
Busca todas as tarefas ativas do Notion e envia relatório por e-mail.
"""

import os
import smtplib
import requests
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── Configuração ────────────────────────────────────────────────────────────────
NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_DESTINO      = os.environ["EMAIL_DESTINO"]

DATABASE_ID = "29bfab6becce8168830ae194246e85cb"

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

_proj_cache = {}


# ── Helpers de API ──────────────────────────────────────────────────────────────

def api_get(path):
    r = requests.get(f"https://api.notion.com/v1/{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()

def api_post(path, payload):
    r = requests.post(f"https://api.notion.com/v1/{path}", headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


# ── Busca de dados ──────────────────────────────────────────────────────────────

def buscar_tarefas():
    tarefas = []
    payload = {
        "filter": {
            "and": [
                {"property": "Status da Tarefa", "status": {"does_not_equal": "Finalizado"}},
                {"property": "Status da Tarefa", "status": {"does_not_equal": "Cancelado"}},
            ]
        },
        "page_size": 100,
    }
    while True:
        data = api_post(f"databases/{DATABASE_ID}/query", payload)
        tarefas.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return tarefas


def buscar_usuarios():
    usuarios = {}
    try:
        data = api_get("users")
        for u in data.get("results", []):
            usuarios[u["id"]] = u.get("name", "")
    except Exception:
        pass
    return usuarios


def buscar_nome_projeto(page_id):
    if not page_id:
        return None
    pid = page_id.replace("-", "")
    if pid in _proj_cache:
        return _proj_cache[pid]
    try:
        page = api_get(f"pages/{pid}")
        for pv in page.get("properties", {}).values():
            if pv.get("type") == "title":
                nome = "".join(x["plain_text"] for x in pv.get("title", []))
                _proj_cache[pid] = nome
                return nome
    except Exception:
        pass
    _proj_cache[pid] = None
    return None


# ── Extração de campos ──────────────────────────────────────────────────────────

def extrair(t, usuarios):
    props = t.get("properties", {})

    def texto(campo):
        v = props.get(campo, {})
        tp = v.get("type")
        if tp == "title":
            return "".join(x["plain_text"] for x in v.get("title", []))
        if tp == "rich_text":
            return "".join(x["plain_text"] for x in v.get("rich_text", []))
        if tp == "select" and v.get("select"):
            return v["select"]["name"]
        if tp == "status" and v.get("status"):
            return v["status"]["name"]
        if tp == "formula":
            f = v.get("formula", {})
            if f.get("type") == "string":
                return f.get("string")
        return None

    def numero(campo):
        v = props.get(campo, {})
        return v.get("number") if v.get("type") == "number" else None

    def data_campo(campo):
        v = props.get(campo, {})
        if v.get("type") == "date" and v.get("date"):
            return v["date"].get("start")
        if v.get("type") == "last_edited_time":
            return v.get("last_edited_time")
        return None

    def checkbox(campo):
        v = props.get(campo, {})
        return bool(v.get("checkbox", False))

    def responsavel():
        v = props.get("Responsável", {})
        pessoas = v.get("people", [])
        if pessoas:
            uid = pessoas[0].get("id", "")
            nome = pessoas[0].get("name") or usuarios.get(uid, "")
            return {"id": uid, "nome": nome}
        return {"id": "", "nome": ""}

    def projeto_id():
        v = props.get("2026 |  Projetos ", {})
        rels = v.get("relation", [])
        return rels[0].get("id", "") if rels else None

    return {
        "id":           t["id"],
        "nome":         texto("Tarefa / Projeto") or "(sem nome)",
        "status":       texto("Status da Tarefa") or "",
        "etapa":        texto("Etapa") or "",
        "observacao":   texto("Observação") or "",
        "d_inicio":     data_campo("D. Início"),
        "dias_uteis":   numero("Dias úteis"),
        "feriados":     numero("Feriados") or 0,
        "responsavel":  responsavel(),
        "ultima_edicao": data_campo("Última edição") or t.get("last_edited_time"),
        "rev_licia1":   checkbox("1° | Lícia "),
        "rev_willian1": checkbox("1° | Willian"),
        "rev_licia2":   checkbox("2° | Lícia"),
        "rev_willian2": checkbox("2° | Willian"),
        "projeto_id":   projeto_id(),
        "projeto_nome": None,
        "url":          t.get("url", ""),
        "d_limite_calc": None,
        "dias_atraso":  None,
        "revisao_info": None,
    }


# ── Cálculo de prazo ────────────────────────────────────────────────────────────

def parse_d(s):
    if not s:
        return None
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).replace(tzinfo=None).date()
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def avancar_dias_uteis(inicio, n):
    atual, contados = inicio, 0
    while contados < n:
        atual += timedelta(days=1)
        if atual.weekday() < 5:
            contados += 1
    return atual


def calcular_prazo(d_inicio_str, dias_uteis, feriados):
    if not d_inicio_str or not dias_uteis:
        return None
    inicio = parse_d(d_inicio_str)
    if not inicio:
        return None
    prazo = avancar_dias_uteis(inicio, int(dias_uteis))
    prazo += timedelta(days=int(feriados or 0))
    return prazo


def fmt_d(d):
    if not d:
        return "—"
    if isinstance(d, str):
        d = parse_d(d)
    return d.strftime("%d/%m") if d else "—"


# ── Fluxo de revisão ────────────────────────────────────────────────────────────

def info_revisao(t):
    l1 = t["rev_licia1"]
    w1 = t["rev_willian1"]
    l2 = t["rev_licia2"]
    w2 = t["rev_willian2"]

    passos = [
        {"label": "1ª Rev. Lícia",    "done": l1},
        {"label": "Correção Willian",  "done": w1},
        {"label": "2ª Rev. Lícia",    "done": l2},
        {"label": "Correção Final",    "done": w2},
    ]

    if w2:
        return {"label": "✅ Revisão concluída — validar Lícia", "aguarda_licia": True,  "concluida": True,  "passos": passos}
    if l2:
        return {"label": "Aguardando correção final (Willian)",  "aguarda_licia": False, "concluida": False, "passos": passos}
    if w1:
        return {"label": "Aguardando 2ª revisão de Lícia",       "aguarda_licia": True,  "concluida": False, "passos": passos}
    if l1:
        return {"label": "Aguardando correção Willian (1ª rod.)", "aguarda_licia": False, "concluida": False, "passos": passos}
    return     {"label": "Aguardando 1ª revisão de Lícia",        "aguarda_licia": True,  "concluida": False, "passos": passos}


# ── Montagem dos dados ──────────────────────────────────────────────────────────

def montar(tarefas):
    hoje = datetime.utcnow().date()
    em3  = hoje + timedelta(days=3)
    em5  = hoje + timedelta(days=5)
    ha3  = hoje - timedelta(days=3)
    ha7  = hoje - timedelta(days=7)

    atrasadas, vencem3 = [], []
    revisoes, rev_paradas, rev_concluidas = [], [], []
    aguarda_licia_ids = set()
    aguarda_licia = []
    licia, willian = [], []
    proj_map = {}

    for t in tarefas:
        nome_resp  = t["responsavel"]["nome"].lower()
        eh_licia   = "lícia" in nome_resp or "licia" in nome_resp
        eh_willian = "willian" in nome_resp

        prazo = calcular_prazo(t["d_inicio"], t["dias_uteis"], t["feriados"])
        t["d_limite_calc"] = prazo
        if prazo:
            if prazo < hoje:
                t["dias_atraso"] = (hoje - prazo).days
                atrasadas.append(t)
            elif prazo <= em3:
                vencem3.append(t)

        if t["status"] == "Revisão":
            ri = info_revisao(t)
            t["revisao_info"] = ri
            revisoes.append(t)

            ultima = parse_d(t["ultima_edicao"])
            if ultima and ultima < ha3:
                t["dias_parado"] = (hoje - ultima).days
                rev_paradas.append(t)

            if ri["concluida"]:
                rev_concluidas.append(t)

            if ri["aguarda_licia"] and t["id"] not in aguarda_licia_ids:
                aguarda_licia_ids.add(t["id"])
                aguarda_licia.append(t)

        if eh_licia:
            licia.append(t)
        elif eh_willian:
            willian.append(t)

        pnome = t["projeto_nome"]
        ultima = parse_d(t["ultima_edicao"])
        if pnome:
            if pnome not in proj_map or (ultima and (proj_map[pnome] is None or ultima > proj_map[pnome])):
                proj_map[pnome] = ultima

    proj_parados = []
    for nome, ultima in proj_map.items():
        if ultima is None or ultima <= ha7:
            dias = (hoje - ultima).days if ultima else None
            ts_proj = [t for t in tarefas if t["projeto_nome"] == nome]
            proj_parados.append({"nome": nome, "dias": dias, "ultima": ultima, "tarefas": ts_proj})
    proj_parados.sort(key=lambda x: -(x["dias"] or 0))

    nao_iniciadas_risco = [
        t for t in tarefas
        if t["status"] == "Não iniciada" and t["d_limite_calc"] and t["d_limite_calc"] <= em5
    ]

    return {
        "hoje":               hoje,
        "total":              len(tarefas),
        "atrasadas":          sorted(atrasadas, key=lambda x: -(x["dias_atraso"] or 0)),
        "vencem3":            sorted(vencem3, key=lambda x: x["d_limite_calc"]),
        "revisoes":           revisoes,
        "rev_paradas":        rev_paradas,
        "rev_concluidas":     rev_concluidas,
        "aguarda_licia":      aguarda_licia,
        "licia":              licia,
        "willian":            willian,
        "proj_parados":       proj_parados,
        "nao_iniciadas_risco": nao_iniciadas_risco,
    }


# ── HTML helpers ────────────────────────────────────────────────────────────────

BADGE_CORES = {
    "Revisão":       ("#92400e", "#fef3c7"),
    "Iniciar":       ("#374151", "#e5e7eb"),
    "Não iniciada":  ("#6b7280", "#f3f4f6"),
    "Detalhamento":  ("#5b21b6", "#ede9fe"),
    "Criação":       ("#1e40af", "#dbeafe"),
    "Com Pendência": ("#713f12", "#fef9c3"),
}


def badge(status):
    c, bg = BADGE_CORES.get(status, ("#555", "#f0f0f0"))
    return (
        f'<span style="display:inline-block;font-size:10px;padding:2px 8px;'
        f'border-radius:99px;font-weight:500;background:{bg};color:{c};">'
        f'{status}</span>'
    )


def rev_flow_html(passos):
    parts = []
    achou_atual = False
    for i, p in enumerate(passos):
        if p["done"]:
            st = "background:#d1fae5;color:#065f46;border:1px solid #a7f3d0;"
        elif not achou_atual:
            achou_atual = True
            st = "background:#fef3c7;color:#92400e;border:1px solid #fde68a;font-weight:600;"
        else:
            st = "background:#f3f4f6;color:#9ca3af;border:1px solid #e5e7eb;"
        parts.append(f'<span style="font-size:10px;padding:2px 7px;border-radius:4px;{st}">{p["label"]}</span>')
        if i < len(passos) - 1:
            parts.append('<span style="color:#d1d5db;font-size:10px;margin:0 2px;">→</span>')
    return (
        '<div style="margin-top:5px;display:flex;gap:2px;flex-wrap:wrap;align-items:center;">'
        + "".join(parts) + "</div>"
    )


def celula_tarefa(t, mostrar_flow=False):
    proj = (f'<span style="font-size:11px;color:#999;display:block;margin-bottom:2px;">'
            f'{t["projeto_nome"] or ""}</span>') if t["projeto_nome"] else ""
    link = (f'<a href="{t["url"]}" style="color:#1a1a1a;text-decoration:none;'
            f'font-size:13px;font-weight:500;">{t["nome"]}</a>')
    etapa = (f'<span style="font-size:11px;color:#bbb;margin-left:4px;">'
             f'· {t["etapa"]}</span>') if t["etapa"] else ""
    obs = (f'<div style="font-size:11px;color:#aaa;font-style:italic;margin-top:3px;">'
           f'{t["observacao"]}</div>') if t["observacao"] else ""
    flow = rev_flow_html(t["revisao_info"]["passos"]) if mostrar_flow and t.get("revisao_info") else ""
    return f'<td style="padding:10px;border-bottom:1px solid #f8f8f8;vertical-align:top;">{proj}{link}{etapa}{obs}{flow}</td>'


def td(conteudo, cor=None, negrito=False, nowrap=True):
    styles = "padding:10px;border-bottom:1px solid #f8f8f8;font-size:13px;vertical-align:top;"
    if cor:
        styles += f"color:{cor};"
    if negrito:
        styles += "font-weight:600;"
    if nowrap:
        styles += "white-space:nowrap;"
    return f'<td style="{styles}">{conteudo}</td>'


def tabela_wrap(linhas_html, colunas):
    ths = "".join(
        f'<th style="text-align:left;font-size:10px;text-transform:uppercase;'
        f'letter-spacing:.5px;color:#aaa;padding:6px 10px;border-bottom:1px solid #f0f0f0;'
        f'font-weight:500;">{c}</th>'
        for c in colunas
    )
    corpo = "".join(linhas_html)
    return (
        '<div style="border:1px solid #eee;border-radius:8px;overflow:hidden;margin-bottom:14px;">'
        '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
        f'<thead><tr>{ths}</tr></thead><tbody>{corpo}</tbody>'
        '</table></div>'
    )


def secao_titulo(t):
    return (
        f'<p style="margin:0 0 14px;font-size:11px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:1.5px;color:#888;padding-bottom:8px;border-bottom:1px solid #f0f0f0;">{t}</p>'
    )


def sub_titulo(t, n=None):
    cnt = (f'<span style="background:#f0f0f0;color:#888;font-size:10px;padding:1px 6px;'
           f'border-radius:99px;font-weight:500;margin-left:6px;">{n}</span>') if n is not None else ""
    return f'<p style="margin:0 0 8px;font-size:12px;font-weight:600;color:#555;">{t}{cnt}</p>'


def alerta(texto, tipo="yellow"):
    cores = {
        "red":    ("fff3f3", "fc8181"),
        "yellow": ("fffbeb", "f6c90e"),
        "blue":   ("ebf4ff", "63b3ed"),
        "green":  ("f0fff4", "68d391"),
    }
    bg, brd = cores.get(tipo, cores["yellow"])
    return (
        f'<div style="background:#{bg};border-left:3px solid #{brd};border-radius:0 6px 6px 0;'
        f'padding:10px 14px;margin-bottom:10px;font-size:13px;">{texto}</div>'
    )


def vazio(msg="Nenhum item encontrado."):
    return f'<p style="color:#bbb;font-size:13px;padding:8px 0;font-style:italic;">{msg}</p>'


def divider():
    return '<hr style="border:none;border-top:1px solid #f0f0f0;margin:26px 0;">'


# ── Geração do HTML ─────────────────────────────────────────────────────────────

def gerar_html(rel):
    hoje      = rel["hoje"]
    hoje_str  = hoje.strftime("%A, %d de %B de %Y").capitalize()
    aguarda_licia_externos = [
        t for t in rel["aguarda_licia"] if t["id"] not in {x["id"] for x in rel["licia"]}
    ]

    n_licia_total = len(rel["licia"]) + len(aguarda_licia_externos)
    cards = (
        '<div style="display:flex;gap:10px;margin-bottom:28px;flex-wrap:wrap;">'
        '<div style="flex:1;min-width:110px;background:#fff3f3;border-radius:8px;padding:14px 16px;">'
        '<p style="margin:0;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#c0392b;">Atrasadas</p>'
        f'<p style="margin:6px 0 0;font-size:26px;font-weight:700;color:#c0392b;">{len(rel["atrasadas"])}</p>'
        '</div>'
        '<div style="flex:1;min-width:110px;background:#fff8f0;border-radius:8px;padding:14px 16px;">'
        '<p style="margin:0;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#c05621;">Revisão parada</p>'
        f'<p style="margin:6px 0 0;font-size:26px;font-weight:700;color:#c05621;">{len(rel["rev_paradas"])}</p>'
        '</div>'
        '<div style="flex:1;min-width:110px;background:#f0f4ff;border-radius:8px;padding:14px 16px;">'
        '<p style="margin:0;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#2c5282;">Dependem Lícia</p>'
        f'<p style="margin:6px 0 0;font-size:26px;font-weight:700;color:#2c5282;">{n_licia_total}</p>'
        '</div>'
        '<div style="flex:1;min-width:110px;background:#f7f7f7;border-radius:8px;padding:14px 16px;">'
        '<p style="margin:0;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#555;">Total ativo</p>'
        f'<p style="margin:6px 0 0;font-size:26px;font-weight:700;color:#1a1a1a;">{rel["total"]}</p>'
        '</div>'
        '</div>'
    )

    # ── Seção 1 — Atenção imediata ───────────────────────────────────────────────
    if rel["atrasadas"]:
        linhas = []
        for t in rel["atrasadas"]:
            atraso_str = str(t["dias_atraso"]) + "d"
            resp_nome = t["responsavel"]["nome"] or "—"
            linhas.append(
                f'<tr>{celula_tarefa(t)}{td(badge(t["status"]))}'
                f'{td(resp_nome, nowrap=True)}'
                f'{td(atraso_str, cor="#e53e3e", negrito=True)}</tr>'
            )
        s1_atrasadas = tabela_wrap(linhas, ["Projeto / Tarefa", "Status", "Responsável", "Atraso"])
    else:
        s1_atrasadas = vazio("Nenhuma tarefa atrasada. Preencha D. Início + Dias úteis para ativar esta seção.")

    if rel["vencem3"]:
        linhas = []
        for t in rel["vencem3"]:
            resp_nome = t["responsavel"]["nome"] or "—"
            linhas.append(
                f'<tr>{celula_tarefa(t)}{td(badge(t["status"]))}'
                f'{td(resp_nome)}'
                f'{td(fmt_d(t["d_limite_calc"]), cor="#c05621")}</tr>'
            )
        s1_vence3 = tabela_wrap(linhas, ["Projeto / Tarefa", "Status", "Responsável", "Limite"])
    else:
        s1_vence3 = vazio("Nenhuma tarefa vence nos próximos 3 dias.")

    alertas_criticos = []
    for t in rel["rev_paradas"]:
        dias_p = t.get("dias_parado", "?")
        alertas_criticos.append(alerta(
            f'<strong>Revisão travada há {dias_p} dias</strong> — '
            f'<a href="{t["url"]}" style="color:#92400e;">{t["nome"]}</a> '
            f'({t["projeto_nome"]}) · {t["revisao_info"]["label"]}', "red"))
    for t in rel["rev_concluidas"]:
        alertas_criticos.append(alerta(
            f'<strong>✅ Revisão concluída por Willian</strong> — '
            f'<a href="{t["url"]}" style="color:#065f46;">{t["nome"]}</a> '
            f'({t["projeto_nome"]}) · Aguarda validação final de Lícia.', "green"))
    for p in rel["proj_parados"][:2]:
        n_tf = len(p["tarefas"])
        alertas_criticos.append(alerta(
            f'<strong>Projeto parado há {p["dias"]} dias</strong> — {p["nome"]} · '
            f'{n_tf} tarefa(s) aberta(s)', "yellow"))
    if not alertas_criticos:
        alertas_criticos.append(alerta("Nenhuma prioridade crítica identificada hoje.", "blue"))

    alertas_criticos_html = "".join(alertas_criticos)
    s1 = (
        '<div style="margin-bottom:26px;">'
        + secao_titulo("Seção 1 — Atenção imediata")
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Atrasadas", len(rel["atrasadas"])) + s1_atrasadas
        + '</div>'
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Vencem nos próximos 3 dias", len(rel["vencem3"])) + s1_vence3
        + '</div>'
        + sub_titulo("Prioridades críticas")
        + alertas_criticos_html
        + '</div>'
    )

    # ── Seção 2 — Revisões ───────────────────────────────────────────────────────
    if rel["rev_paradas"]:
        linhas = []
        for t in rel["rev_paradas"]:
            ri = t["revisao_info"]
            aguarda = "Lícia" if ri["aguarda_licia"] else "Willian"
            parado_str = str(t.get("dias_parado", "?")) + "d"
            linhas.append(
                f'<tr>{celula_tarefa(t, mostrar_flow=True)}'
                f'{td(t["etapa"])}'
                f'{td(aguarda, cor="#c0392b", negrito=True)}'
                f'{td(parado_str, cor="#e53e3e", negrito=True)}</tr>'
            )
        s2_paradas = tabela_wrap(linhas, ["Projeto / Tarefa", "Etapa", "Aguardando", "Parado há"])
    else:
        s2_paradas = vazio("Nenhuma revisão parada há mais de 3 dias.")

    if rel["revisoes"]:
        linhas = []
        for t in rel["revisoes"]:
            ri = t["revisao_info"]
            concluida_tag = (' <span style="background:#d1fae5;color:#065f46;font-size:10px;'
                             'padding:1px 6px;border-radius:99px;">Concluída</span>') if ri["concluida"] else ""
            label_td = td(ri["label"] + concluida_tag, nowrap=False)
            linhas.append(f'<tr>{celula_tarefa(t, mostrar_flow=True)}{label_td}</tr>')
        s2_fluxo = tabela_wrap(linhas, ["Projeto / Tarefa", "Etapa do fluxo"])
    else:
        s2_fluxo = vazio("Nenhuma tarefa em revisão no momento.")

    s2 = (
        '<div style="margin-bottom:26px;">'
        + secao_titulo("Seção 2 — Revisões")
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("⚠️ Em revisão há mais de 3 dias sem validação", len(rel["rev_paradas"])) + s2_paradas
        + '</div>'
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Fluxo atual de revisões", len(rel["revisoes"])) + s2_fluxo
        + '</div>'
        + '</div>'
    )

    # ── Seção 3 — Dependem da Lícia ──────────────────────────────────────────────
    if rel["licia"]:
        linhas = []
        for t in rel["licia"]:
            ultima = parse_d(t["ultima_edicao"])
            dias_sem_mov = (hoje - ultima).days if ultima else None
            sem_mov_str = (str(dias_sem_mov) + "d") if dias_sem_mov is not None else "—"
            prazo_str = fmt_d(t["d_limite_calc"]) if t["d_limite_calc"] else "—"
            linhas.append(
                f'<tr>{celula_tarefa(t, mostrar_flow=(t["status"] == "Revisão"))}'
                f'{td(badge(t["status"]))}'
                f'{td(t["etapa"] or "—")}'
                f'{td(prazo_str)}'
                f'{td(sem_mov_str, cor="#888")}</tr>'
            )
        s3_licia = tabela_wrap(linhas, ["Projeto / Tarefa", "Status", "Etapa", "Prazo", "Sem mov."])
    else:
        s3_licia = vazio("Nenhuma tarefa ativa para Lícia.")

    if aguarda_licia_externos:
        linhas = []
        for t in aguarda_licia_externos:
            ri = t["revisao_info"]
            ultima = parse_d(t["ultima_edicao"])
            dias_ag = (hoje - ultima).days if ultima else None
            dias_ag_str = (str(dias_ag) + "d") if dias_ag is not None else "—"
            linhas.append(
                f'<tr>{celula_tarefa(t, mostrar_flow=True)}'
                f'{td(ri["label"], nowrap=False)}'
                f'{td(dias_ag_str, cor="#c0392b", negrito=True)}</tr>'
            )
        s3_aguarda = tabela_wrap(linhas, ["Projeto / Tarefa", "Tipo de pendência", "Aguardando há"])
    else:
        s3_aguarda = vazio("Nenhuma tarefa de Willian aguardando ação de Lícia no momento.")

    s3 = (
        '<div style="margin-bottom:26px;">'
        + secao_titulo("Seção 3 — Dependem da Lícia")
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Tarefas ativas — responsável: Lícia", len(rel["licia"])) + s3_licia
        + '</div>'
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Tarefas de Willian aguardando ação de Lícia", len(aguarda_licia_externos)) + s3_aguarda
        + '</div>'
        + '</div>'
    )

    # ── Seção 4 — Pode virar problema ────────────────────────────────────────────
    if rel["proj_parados"]:
        linhas = []
        for p in rel["proj_parados"]:
            cor_dias = "#e53e3e" if (p["dias"] or 0) >= 14 else "#c05621"
            statuses = ", ".join(sorted(set(t["status"] for t in p["tarefas"])))
            responsaveis = ", ".join(sorted(set(
                t["responsavel"]["nome"] for t in p["tarefas"] if t["responsavel"]["nome"]
            )))
            nome_bold = "<strong>" + p["nome"] + "</strong>"
            dias_str = str(p["dias"]) + "d"
            n_tf = len(p["tarefas"])
            tarefas_html = (
                f'{n_tf} tarefa(s)<br>'
                f'<span style="color:#bbb;font-size:11px;">{statuses}</span>'
            )
            linhas.append(
                f'<tr>'
                f'{td(nome_bold, nowrap=False)}'
                f'{td(dias_str, cor=cor_dias, negrito=True)}'
                f'{td(fmt_d(p["ultima"]))}'
                f'{td(tarefas_html, nowrap=False)}'
                f'{td(responsaveis or "—", cor="#888")}'
                f'</tr>'
            )
        s4_parados = tabela_wrap(linhas, ["Projeto", "Parado há", "Última ativ.", "Tarefas abertas", "Responsável(is)"])
    else:
        s4_parados = vazio("Nenhum projeto parado há mais de 7 dias.")

    if rel["nao_iniciadas_risco"]:
        linhas = []
        for t in rel["nao_iniciadas_risco"]:
            resp_nome = t["responsavel"]["nome"] or "—"
            linhas.append(
                f'<tr>{celula_tarefa(t)}'
                f'{td(resp_nome)}'
                f'{td(fmt_d(t["d_limite_calc"]), cor="#c05621", negrito=True)}</tr>'
            )
        s4_risco = tabela_wrap(linhas, ["Projeto / Tarefa", "Responsável", "Limite"])
    else:
        s4_risco = vazio("Nenhuma tarefa não iniciada com prazo nos próximos 5 dias.")

    gargalos = []
    nao_inic_licia = sum(1 for t in rel["licia"] if t["status"] in ("Não iniciada", "Iniciar"))
    if nao_inic_licia >= 3:
        gargalos.append(alerta(
            f'<strong>{nao_inic_licia} tarefas "Não iniciada" ou "Iniciar"</strong> concentradas em Lícia. '
            f'Risco de acúmulo. Defina D. Início + Dias úteis para ativar controle de prazos.', "yellow"))
    if len(rel["rev_paradas"]) >= 2:
        gargalos.append(alerta(
            f'<strong>{len(rel["rev_paradas"])} revisões paradas simultaneamente.</strong> '
            f'Gargalo no fluxo de qualidade do escritório.', "red"))
    if len(rel["proj_parados"]) >= 3:
        gargalos.append(alerta(
            f'<strong>{len(rel["proj_parados"])} projetos parados.</strong> '
            f'Verificar se há dependências externas (cliente, fornecedor) bloqueando.', "yellow"))
    if not gargalos:
        gargalos.append(alerta("Nenhum gargalo crítico além dos já listados acima.", "blue"))

    gargalos_html = "".join(gargalos)
    s4 = (
        '<div style="margin-bottom:26px;">'
        + secao_titulo("Seção 4 — Pode virar problema")
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Projetos parados há mais de 7 dias", len(rel["proj_parados"])) + s4_parados
        + '</div>'
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Não iniciadas com prazo próximo (≤ 5 dias)", len(rel["nao_iniciadas_risco"])) + s4_risco
        + '</div>'
        + sub_titulo("Gargalos identificados")
        + gargalos_html
        + '</div>'
    )

    # ── Seção 5 — Visão Willian ──────────────────────────────────────────────────
    if rel["willian"]:
        linhas = []
        for t in rel["willian"]:
            ri = t.get("revisao_info")
            flow_cell = celula_tarefa(t, mostrar_flow=(t["status"] == "Revisão"))
            prazo_str = fmt_d(t["d_limite_calc"]) if t["d_limite_calc"] else "—"
            if t["status"] == "Revisão" and ri and ri["aguarda_licia"]:
                ultima = parse_d(t["ultima_edicao"])
                dias_b = (hoje - ultima).days if ultima else "?"
                bloqueio = f'<span style="color:#c0392b;font-size:12px;">Aguarda Lícia · {dias_b}d</span>'
            else:
                bloqueio = "—"
            linhas.append(
                f'<tr>{flow_cell}'
                f'{td(badge(t["status"]))}'
                f'{td(t["etapa"] or "—")}'
                f'{td(prazo_str)}'
                f'{td(bloqueio, nowrap=False)}</tr>'
            )
        s5_willian = tabela_wrap(linhas, ["Projeto / Tarefa", "Status", "Etapa", "Prazo", "Bloqueio"])
    else:
        s5_willian = vazio("Nenhuma tarefa ativa para Willian.")

    n_willian = len(rel["willian"])
    n_willian_rev = sum(1 for t in rel["willian"] if t["status"] == "Revisão")
    if n_willian >= 6:
        sobrecarga = alerta(
            f'<strong>Willian com {n_willian} tarefas ativas.</strong> Verificar capacidade.', "yellow")
    elif n_willian_rev >= 2:
        sobrecarga = alerta(
            f'<strong>{n_willian_rev} tarefas de Willian em Revisão simultaneamente.</strong> '
            f'Verificar se alguma está acumulando ciclos.', "yellow")
    elif n_willian == 0:
        sobrecarga = alerta("Willian sem tarefas ativas. Verificar se há novas tarefas a designar.", "blue")
    else:
        sobrecarga = alerta(
            f'<strong>Willian com carga controlada — {n_willian} tarefa(s) ativa(s).</strong>', "green")

    s5 = (
        '<div style="margin-bottom:26px;">'
        + secao_titulo("Seção 5 — Visão Willian")
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Tarefas ativas — responsável: Willian", n_willian) + s5_willian
        + '</div>'
        + sobrecarga
        + '</div>'
    )

    # ── Seção 6 — Resumo executivo ───────────────────────────────────────────────
    pontos = []

    if rel["atrasadas"]:
        nomes = ", ".join(t["nome"] for t in rel["atrasadas"][:3])
        sufixo = "..." if len(rel["atrasadas"]) > 3 else ""
        pontos.append(alerta(
            f'<strong>Tarefas atrasadas ({len(rel["atrasadas"])}):</strong> {nomes}{sufixo}', "red"))

    if rel["rev_paradas"]:
        p = rel["rev_paradas"][0]
        dias_p = p.get("dias_parado", "?")
        pontos.append(alerta(
            f'<strong>Revisão crítica:</strong> {p["nome"]} ({p["projeto_nome"]}) parada há '
            f'{dias_p} dias. {p["revisao_info"]["label"]}.', "red"))

    if rel["rev_concluidas"]:
        for t in rel["rev_concluidas"]:
            pontos.append(alerta(
                f'<strong>✅ Ação necessária:</strong> {t["nome"]} ({t["projeto_nome"]}) — '
                f'Willian concluiu todas as correções. Lícia deve validar e finalizar.', "green"))

    if rel["proj_parados"]:
        maior = rel["proj_parados"][0]
        pontos.append(alerta(
            f'<strong>Maior risco de abandono:</strong> {maior["nome"]} sem atividade há '
            f'{maior["dias"]} dias. Verificar dependências ou retomar com o cliente.', "yellow"))

    n_total_licia = len(rel["licia"]) + len(aguarda_licia_externos)
    pontos.append(alerta(
        f'<strong>Gargalo Lícia:</strong> {len(rel["licia"])} tarefa(s) como responsável + '
        f'{len(aguarda_licia_externos)} aguardando revisão de Lícia = {n_total_licia} itens dependentes.', "blue"))

    if not rel["atrasadas"] and not rel["rev_paradas"] and not rel["rev_concluidas"] and not rel["proj_parados"]:
        pontos = [alerta("✅ Escritório em dia. Nenhum item crítico identificado.", "green")]

    s6 = (
        '<div style="margin-bottom:16px;">'
        + secao_titulo("Seção 6 — Resumo executivo")
        + "".join(pontos)
        + '</div>'
    )

    # ── HTML final ───────────────────────────────────────────────────────────────
    corpo = (
        cards
        + s1 + divider()
        + s2 + divider()
        + s3 + divider()
        + s4 + divider()
        + s5 + divider()
        + s6
        + '<p style="text-align:right;font-size:11px;color:#ccc;margin:24px 0 0;">Gerado automaticamente · Simplific</p>'
    )
    return (
        '<!DOCTYPE html><html>'
        '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#f0f0f0;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;color:#1a1a1a;">'
        '<div style="max-width:720px;margin:32px auto;">'
        '<div style="background:#1a1a1a;padding:24px 28px;border-radius:10px 10px 0 0;">'
        '<p style="margin:0;color:#666;font-size:11px;text-transform:uppercase;letter-spacing:1.5px;">Relatório Operacional · Escritório</p>'
        f'<p style="margin:6px 0 0;color:#fff;font-size:20px;font-weight:600;">{hoje_str}</p>'
        f'<p style="margin:4px 0 0;color:#888;font-size:12px;">{rel["total"]} tarefas ativas · Gerado automaticamente</p>'
        '</div>'
        f'<div style="background:#fff;padding:28px;border-radius:0 0 10px 10px;">{corpo}</div>'
        '</div></body></html>'
    )


# ── Envio de e-mail ─────────────────────────────────────────────────────────────

def enviar(html, hoje):
    assunto = f"Relatório Operacional | {hoje.strftime('%d/%m')} — escritório"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_DESTINO
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.sendmail(GMAIL_USER, EMAIL_DESTINO, msg.as_string())


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    print("Buscando usuários...")
    usuarios = buscar_usuarios()

    print("Buscando tarefas...")
    raw = buscar_tarefas()
    print(f"{len(raw)} tarefas encontradas.")

    print("Extraindo campos...")
    tarefas = [extrair(t, usuarios) for t in raw]

    print("Buscando nomes dos projetos...")
    ids_vistos = set()
    for t in tarefas:
        pid = t["projeto_id"]
        if not pid:
            continue
        if pid not in ids_vistos:
            ids_vistos.add(pid)
            t["projeto_nome"] = buscar_nome_projeto(pid)
        else:
            t["projeto_nome"] = _proj_cache.get(pid.replace("-", ""))
    print(f"{len(ids_vistos)} projetos únicos carregados.")

    print("Montando relatório...")
    rel = montar(tarefas)

    print("Gerando HTML...")
    html = gerar_html(rel)

    print("Enviando e-mail...")
    enviar(html, rel["hoje"])
    print("✓ Relatório enviado com sucesso.")


if __name__ == "__main__":
    main()
