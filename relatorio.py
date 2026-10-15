#!/usr/bin/env python3
"""
Relatório Diário de Produção — Simplific
Busca todas as tarefas ativas do Notion e envia relatório por e-mail.
"""

import os
import smtplib
import requests
from collections import OrderedDict
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

MESES_PT = ["janeiro", "fevereiro", "março", "abril", "maio", "junho",
            "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
DIAS_PT   = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
             "sexta-feira", "sábado", "domingo"]

STATUS_PROJETO_INATIVO = {"finalizado", "cancelado", "arquivado", "concluído", "concluido", "suspenso"}
PROJETO_TEMPLATE = "projeto base"



def fmt_data_pt(d):
    return f"{DIAS_PT[d.weekday()].capitalize()}, {d.day:02d} de {MESES_PT[d.month - 1]} de {d.year}"


def projeto_inativo(status_str):
    if not status_str:
        return False
    s = status_str.lower()
    return any(palavra in s for palavra in STATUS_PROJETO_INATIVO)


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
                {"property": "Status da Tarefa", "status": {"does_not_equal": "Não iniciada"}},
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
        for campo in ("Projeto", "2026 |  Projetos ", "2026 | Projetos",
                      "Projetos", "projeto", "Project"):
            v = props.get(campo, {})
            if v.get("type") == "relation":
                rels = v.get("relation", [])
                if rels:
                    return rels[0].get("id") or None
        for k, v in props.items():
            if v.get("type") == "relation":
                rels = v.get("relation", [])
                if rels:
                    return rels[0].get("id") or None
        return None

    def ler_projeto():
        for campo in ("Projeto", "projeto", "Project", "Nome do projeto",
                      "Projeto / Cliente", "2026 | Projetos", "2026 |  Projetos "):
            v = props.get(campo)
            if not v:
                continue
            tp = v.get("type")
            if tp == "rollup":
                ro = v.get("rollup", {})
                if ro.get("type") == "string" and ro.get("string"):
                    return ro["string"]
                for item in ro.get("array", []):
                    for sub_tp in ("title", "rich_text"):
                        parts = item.get(sub_tp, [])
                        if parts:
                            txt = "".join(x["plain_text"] for x in parts)
                            if txt:
                                return txt
            if tp == "title":
                return "".join(x["plain_text"] for x in v.get("title", [])) or None
            if tp == "rich_text":
                return "".join(x["plain_text"] for x in v.get("rich_text", [])) or None
            if tp == "select" and v.get("select"):
                return v["select"]["name"]
            if tp == "formula":
                f = v.get("formula", {})
                if f.get("type") == "string" and f.get("string"):
                    return f["string"]
        for k, v in props.items():
            if v.get("type") != "rollup":
                continue
            ro = v.get("rollup", {})
            if ro.get("type") == "string" and ro.get("string"):
                return ro["string"]
            for item in ro.get("array", []):
                for sub_tp in ("title", "rich_text"):
                    parts = item.get(sub_tp, [])
                    if parts:
                        txt = "".join(x["plain_text"] for x in parts)
                        if txt:
                            return txt
        return None

    def ler_status_projeto():
        for campo in ("Status do Projeto", "Status Projeto", "Status do projeto",
                      "status do projeto", "Project Status"):
            v = props.get(campo)
            if not v:
                continue
            tp = v.get("type")
            if tp == "rollup":
                ro = v.get("rollup", {})
                if ro.get("type") == "string" and ro.get("string"):
                    return ro["string"]
                for item in ro.get("array", []):
                    if item.get("type") == "status" and item.get("status"):
                        return item["status"]["name"]
                    if item.get("type") == "select" and item.get("select"):
                        return item["select"]["name"]
            if tp in ("select", "status"):
                val = v.get(tp) or {}
                return val.get("name") or None
            if tp == "formula":
                f = v.get("formula", {})
                if f.get("type") == "string" and f.get("string"):
                    return f["string"]
        return None

    return {
        "id":              t["id"],
        "nome":            texto("Tarefa / Projeto") or "(sem nome)",
        "status":          texto("Status da Tarefa") or "",
        "etapa":           texto("Etapa") or "",
        "observacao":      texto("Observação") or "",
        "d_inicio":        data_campo("D. Início"),
        "dias_uteis":      numero("Dias úteis"),
        "feriados":        numero("Feriados") or 0,
        "responsavel":     responsavel(),
        "ultima_edicao":   data_campo("Última edição") or t.get("last_edited_time"),
        "rev_licia1":      checkbox("1° | Lícia "),
        "rev_willian1":    checkbox("1° | Willian"),
        "rev_licia2":      checkbox("2° | Lícia"),
        "rev_willian2":    checkbox("2° | Willian"),
        "projeto_id":      projeto_id(),
        "projeto_nome":    ler_projeto(),
        "projeto_status":  ler_status_projeto(),
        "url":             t.get("url", ""),
        "d_limite_calc":   None,
        "dias_atraso":     None,
        "revisao_info":    None,
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
    em7  = hoje + timedelta(days=7)
    ha7  = hoje - timedelta(days=7)

    atrasadas, vencem7 = [], []
    revisoes = []
    aguarda_licia_rev   = []
    aguarda_willian_rev = []
    deps_externas = []
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
            elif prazo <= em7:
                vencem7.append(t)

        if t["status"] == "Revisão":
            ri = info_revisao(t)
            t["revisao_info"] = ri
            revisoes.append(t)
            if ri["aguarda_licia"]:
                aguarda_licia_rev.append(t)
            else:
                aguarda_willian_rev.append(t)

        if t["status"] == "Com Pendência":
            deps_externas.append(t)

        if eh_licia:
            licia.append(t)
        elif eh_willian:
            willian.append(t)

        pnome = t["projeto_nome"]
        ultima = parse_d(t["ultima_edicao"])
        if pnome:
            if pnome not in proj_map or (ultima and (proj_map[pnome] is None or ultima > proj_map[pnome])):
                proj_map[pnome] = ultima

    proj_sem_mov = []
    for nome, ultima in proj_map.items():
        if ultima is None or ultima <= ha7:
            dias = (hoje - ultima).days if ultima else None
            ts_proj = [t for t in tarefas if t["projeto_nome"] == nome]
            proj_sem_mov.append({"nome": nome, "dias": dias, "ultima": ultima, "tarefas": ts_proj})
    proj_sem_mov.sort(key=lambda x: -(x["dias"] or 0))

    proj_risco = set(t["projeto_nome"] for t in atrasadas if t["projeto_nome"])

    return {
        "hoje":               hoje,
        "total":              len(tarefas),
        "atrasadas":          sorted(atrasadas, key=lambda x: -(x["dias_atraso"] or 0)),
        "vencem7":            sorted(vencem7, key=lambda x: x["d_limite_calc"]),
        "revisoes":           revisoes,
        "aguarda_licia_rev":  aguarda_licia_rev,
        "aguarda_willian_rev": aguarda_willian_rev,
        "deps_externas":      deps_externas,
        "licia":              licia,
        "willian":            willian,
        "proj_sem_mov":       proj_sem_mov,
        "proj_risco":         proj_risco,
    }


# ── Seleção de prioridades ──────────────────────────────────────────────────────

def selecionar_prioridades(rel):
    hoje = rel["hoje"]
    projetos = {}

    def get_proj(nome):
        if nome not in projetos:
            projetos[nome] = {
                "nome": nome,
                "atrasadas": [], "vencem7": [],
                "aguarda_licia": [], "aguarda_willian": [],
                "representante": None,
            }
        return projetos[nome]

    for t in rel["atrasadas"]:
        p = get_proj(t.get("projeto_nome") or "Sem projeto")
        p["atrasadas"].append(t)
        if not p["representante"]:
            p["representante"] = t

    for t in rel["vencem7"]:
        p = get_proj(t.get("projeto_nome") or "Sem projeto")
        p["vencem7"].append(t)
        if not p["representante"]:
            p["representante"] = t

    for t in rel["aguarda_licia_rev"]:
        p = get_proj(t.get("projeto_nome") or "Sem projeto")
        p["aguarda_licia"].append(t)
        if not p["representante"]:
            p["representante"] = t

    for t in rel["aguarda_willian_rev"]:
        p = get_proj(t.get("projeto_nome") or "Sem projeto")
        p["aguarda_willian"].append(t)
        if not p["representante"]:
            p["representante"] = t

    candidatos = []
    for pn, p in projetos.items():
        if not (p["atrasadas"] or p["vencem7"] or p["aguarda_licia"] or p["aguarda_willian"]):
            continue
        peso = 0
        tipo_principal = None

        if p["atrasadas"]:
            max_atraso = max((t.get("dias_atraso") or 0) for t in p["atrasadas"])
            n = len(p["atrasadas"])
            peso_a = 100 + min(max_atraso, 30) + min(n * 3, 15)
            if peso_a > peso:
                peso = peso_a
                tipo_principal = "atrasada"

        if p["vencem7"]:
            prazos = [t["d_limite_calc"] for t in p["vencem7"] if t["d_limite_calc"]]
            prazo_min = min(prazos) if prazos else None
            dias_rest = (prazo_min - hoje).days if prazo_min else 7
            n = len(p["vencem7"])
            peso_v = 80 + (7 - dias_rest) * 2 + min(n * 2, 6)
            if peso_v > peso:
                peso = peso_v
                tipo_principal = tipo_principal or "prazo"

        if not p["atrasadas"] and not p["vencem7"]:
            if p["aguarda_licia"]:
                n = len(p["aguarda_licia"])
                peso_r = 40 + min(n * 2, 8)
                if peso_r > peso:
                    peso = peso_r
                    tipo_principal = "revisao_licia"
            elif p["aguarda_willian"]:
                n = len(p["aguarda_willian"])
                peso_r = 35 + min(n * 2, 8)
                if peso_r > peso:
                    peso = peso_r
                    tipo_principal = "revisao_willian"

        if tipo_principal:
            candidatos.append({
                "peso": peso, "tipo": tipo_principal,
                "projeto": pn, "proj_data": p,
                "representante": p["representante"],
            })

    candidatos.sort(key=lambda x: -x["peso"])

    resultado = []
    for c in candidatos:
        tem_prazo = bool(c["proj_data"]["atrasadas"] or c["proj_data"]["vencem7"])
        if tem_prazo or c["peso"] > 42:
            resultado.append(c)
        if len(resultado) == 5:
            break
    return resultado


# ── HTML helpers ────────────────────────────────────────────────────────────────

BADGE_CORES = {
    "Revisão":       ("#92400e", "#fef3c7"),
    "Iniciar":       ("#374151", "#e5e7eb"),
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
    link = (f'<a href="{t["url"]}" style="color:#1a1a1a;text-decoration:none;'
            f'font-size:13px;font-weight:500;">{t["nome"]}</a>')
    etapa = (f'<span style="font-size:11px;color:#bbb;margin-left:4px;">'
             f'· {t["etapa"]}</span>') if t["etapa"] else ""
    obs = (f'<div style="font-size:11px;color:#aaa;font-style:italic;margin-top:3px;">'
           f'{t["observacao"]}</div>') if t["observacao"] else ""
    flow = rev_flow_html(t["revisao_info"]["passos"]) if mostrar_flow and t.get("revisao_info") else ""
    return f'<td style="padding:10px;border-bottom:1px solid #f8f8f8;vertical-align:top;">{link}{etapa}{obs}{flow}</td>'


def td(conteudo, cor=None, negrito=False, nowrap=True):
    styles = "padding:10px;border-bottom:1px solid #f8f8f8;font-size:13px;vertical-align:top;"
    if cor:
        styles += f"color:{cor};"
    if negrito:
        styles += "font-weight:600;"
    if nowrap:
        styles += "white-space:nowrap;"
    return f'<td style="{styles}">{conteudo}</td>'


def header_projeto(nome, ncols):
    return (
        f'<tr style="background:#f4f4f4;">'
        f'<td colspan="{ncols}" style="padding:7px 10px;font-size:11px;font-weight:700;'
        f'color:#444;border-bottom:1px solid #e8e8e8;letter-spacing:.2px;">'
        f'📁 {nome}</td></tr>'
    )


def agrupar_por_projeto(lista):
    grupos = OrderedDict()
    for t in lista:
        proj = t.get("projeto_nome") or "Sem projeto"
        if proj not in grupos:
            grupos[proj] = []
        grupos[proj].append(t)
    return list(grupos.items())


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
    hoje     = rel["hoje"]
    hoje_str = fmt_data_pt(hoje)

    # ── Seção 1 — Resumo Gerencial e Prioridades ─────────────────────────────────

    def card_indicador(label, valor, cor_label, cor_valor, cor_fundo):
        return (
            f'<div style="flex:1;min-width:110px;background:{cor_fundo};'
            f'border-radius:8px;padding:14px 16px;">'
            f'<p style="margin:0;font-size:10px;text-transform:uppercase;'
            f'letter-spacing:.5px;color:{cor_label};">{label}</p>'
            f'<p style="margin:6px 0 0;font-size:26px;font-weight:700;color:{cor_valor};">{valor}</p>'
            '</div>'
        )

    cards = (
        '<div style="display:flex;gap:10px;margin-bottom:28px;flex-wrap:wrap;">'
        + card_indicador("Total ativo",       rel["total"],             "#555",    "#1a1a1a", "#f7f7f7")
        + card_indicador("Atrasadas",         len(rel["atrasadas"]),    "#c0392b", "#c0392b", "#fff3f3")
        + card_indicador("Em revisão",        len(rel["revisoes"]),     "#92400e", "#92400e", "#fffbeb")
        + card_indicador("Entregas 7 dias",   len(rel["vencem7"]),      "#1e40af", "#1e40af", "#eff6ff")
        + card_indicador("Projetos c/ risco", len(rel["proj_risco"]),   "#5b21b6", "#5b21b6", "#f5f3ff")
        + '</div>'
    )

    TIPO_BADGE = {
        "atrasada":        ("#fff",    "#c0392b", "Atrasada"),
        "prazo":           ("#fff",    "#c05621", "Prazo próximo"),
        "revisao_licia":   ("#92400e", "#fef3c7", "Aguarda Lícia"),
        "revisao_willian": ("#1e40af", "#dbeafe", "Aguarda Willian"),
    }

    def _proxima_acao_prio(pd):
        if pd["aguarda_licia"]:
            extra = f" para liberar {len(pd['aguarda_licia'])} tarefa(s) de Willian" if pd["aguarda_willian"] else ""
            return f"Lícia revisar{extra}"
        if pd["aguarda_willian"]:
            return f"Willian corrigir {len(pd['aguarda_willian'])} tarefa(s)"
        if pd["atrasadas"]:
            resps = sorted(set(t["responsavel"]["nome"] for t in pd["atrasadas"] if t["responsavel"]["nome"]))
            return f"{' / '.join(resps) or '—'} retomar execução"
        if pd["vencem7"]:
            resps = sorted(set(t["responsavel"]["nome"] for t in pd["vencem7"] if t["responsavel"]["nome"]))
            return f"{' / '.join(resps) or '—'} priorizar entrega"
        return "—"

    def _situacao_linhas_prio(p, pd):
        linhas = []
        if pd["atrasadas"]:
            max_at = max((t.get("dias_atraso") or 0) for t in pd["atrasadas"])
            n = len(pd["atrasadas"])
            linhas.append(f'{n} tarefa(s) atrasada(s) · há {max_at}d')
        if pd["vencem7"]:
            prazos = [t["d_limite_calc"] for t in pd["vencem7"] if t["d_limite_calc"]]
            prazo_min = min(prazos) if prazos else None
            dias_rest = (prazo_min - hoje).days if prazo_min else None
            suffix = f" · {dias_rest}d para entrega" if dias_rest is not None else ""
            linhas.append(f'{len(pd["vencem7"])} tarefa(s) com prazo próximo{suffix}')
        at_ids = {t["id"] for t in pd["atrasadas"]}
        v7_ids = {t["id"] for t in pd["vencem7"]}
        licia_extras   = [t for t in pd["aguarda_licia"]   if t["id"] not in at_ids and t["id"] not in v7_ids]
        willian_extras = [t for t in pd["aguarda_willian"] if t["id"] not in at_ids and t["id"] not in v7_ids]
        if licia_extras:
            linhas.append(f'{len(licia_extras)} revisão(ões) aguardando Lícia')
        if willian_extras:
            linhas.append(f'{len(willian_extras)} correção(ões) aguardando Willian')
        return linhas

    prioridades = selecionar_prioridades(rel)
    if prioridades:
        itens_prio = []
        for i, p in enumerate(prioridades, 1):
            pd = p["proj_data"]
            cor_txt, cor_bg, lbl_tipo = TIPO_BADGE.get(p["tipo"], ("#555", "#f0f0f0", p["tipo"]))
            rep = p["representante"]
            url = rep["url"] if rep else "#"
            sit_linhas = _situacao_linhas_prio(p, pd)
            sit_html = " &nbsp;·&nbsp; ".join(
                f'<span style="color:#555;">{l}</span>' for l in sit_linhas
            )
            acao = _proxima_acao_prio(pd)
            itens_prio.append(
                f'<div style="display:flex;align-items:flex-start;gap:12px;padding:10px 0;'
                f'border-bottom:1px solid #f8f8f8;">'
                f'<span style="min-width:22px;height:22px;background:#1a1a1a;color:#fff;'
                f'border-radius:50%;display:inline-flex;align-items:center;justify-content:center;'
                f'font-size:10px;font-weight:700;flex-shrink:0;">{i}</span>'
                f'<div style="min-width:0;">'
                f'<div style="margin-bottom:3px;">'
                f'<span style="font-size:10px;padding:1px 7px;border-radius:4px;'
                f'background:{cor_bg};color:{cor_txt};font-weight:600;margin-right:6px;">{lbl_tipo}</span>'
                f'<a href="{url}" style="color:#1a1a1a;text-decoration:none;'
                f'font-size:13px;font-weight:600;">{p["projeto"]}</a>'
                f'</div>'
                f'<div style="font-size:11px;color:#888;margin-top:2px;">{sit_html}</div>'
                f'<div style="font-size:11px;color:#555;margin-top:4px;">'
                f'Próxima ação: <strong>{acao}</strong></div>'
                f'</div></div>'
            )
        prioridades_html = '<div>' + "".join(itens_prio) + '</div>'
    else:
        prioridades_html = alerta("✅ Nenhuma prioridade crítica identificada para o próximo dia útil.", "green")

    s1 = (
        '<div style="margin-bottom:26px;">'
        + secao_titulo("Seção 1 — Resumo Gerencial e Prioridades")
        + cards
        + sub_titulo("Prioridades do próximo dia útil")
        + prioridades_html
        + '</div>'
    )

    # ── Seção 2 — Gargalos e Pendências ──────────────────────────────────────────

    def resumo_por_projeto(grupos_tipo):
        """grupos_tipo: lista de (label, badge_html, lista_tarefas)"""
        linhas = []
        for label, badge_html, tarefas in grupos_tipo:
            for proj, grupo in agrupar_por_projeto(tarefas):
                prazos = [t["d_limite_calc"] for t in grupo if t["d_limite_calc"]]
                prazo_min = min(prazos) if prazos else None
                cor_prazo = "#e53e3e" if any((t.get("dias_atraso") or 0) > 0 for t in grupo) else "#555"
                url = grupo[0]["url"]
                nome_link = (
                    f'<a href="{url}" style="color:#1a1a1a;text-decoration:none;'
                    f'font-size:13px;font-weight:500;">{proj}</a>'
                )
                linhas.append(
                    f'<tr>'
                    f'{td(nome_link, nowrap=False)}'
                    f'<td style="padding:10px;border-bottom:1px solid #f8f8f8;vertical-align:top;">'
                    f'{badge_html}</td>'
                    f'{td(str(len(grupo)), negrito=True)}'
                    f'{td(fmt_d(prazo_min) if prazo_min else "—", cor=cor_prazo)}'
                    f'</tr>'
                )
        return tabela_wrap(linhas, ["Projeto", "Tipo", "Quantidade", "Prazo"])

    BADGE_LICIA   = '<span style="font-size:10px;padding:1px 7px;border-radius:4px;background:#fef3c7;color:#92400e;font-weight:600;">Revisão Lícia</span>'
    TIPO_CORR     = '<span style="font-size:10px;padding:1px 7px;border-radius:4px;background:#dbeafe;color:#1e40af;font-weight:600;">Correção</span>'
    TIPO_EXEC     = '<span style="font-size:10px;padding:1px 7px;border-radius:4px;background:#e5e7eb;color:#374151;font-weight:600;">Execução</span>'

    if rel["aguarda_licia_rev"]:
        s2_licia = resumo_por_projeto([("Revisão Lícia", BADGE_LICIA, rel["aguarda_licia_rev"])])
    else:
        s2_licia = vazio("Nenhuma revisão aguardando Lícia no momento.")

    TIPO_CORR = '<span style="font-size:10px;padding:1px 7px;border-radius:4px;background:#dbeafe;color:#1e40af;font-weight:600;">Correção</span>'
    TIPO_EXEC = '<span style="font-size:10px;padding:1px 7px;border-radius:4px;background:#e5e7eb;color:#374151;font-weight:600;">Execução</span>'

    willian_exec = [t for t in rel["willian"] if t["status"] != "Revisão"]
    willian_total = len(rel["aguarda_willian_rev"]) + len(willian_exec)

    if willian_total:
        s2_willian = resumo_por_projeto([
            ("Correção", TIPO_CORR, rel["aguarda_willian_rev"]),
            ("Execução",  TIPO_EXEC, willian_exec),
        ])
    else:
        s2_willian = vazio("Nenhuma tarefa pendente com Willian no momento.")

    if rel["deps_externas"]:
        linhas = []
        for proj, grupo in agrupar_por_projeto(rel["deps_externas"]):
            linhas.append(header_projeto(proj, 3))
            for t in grupo:
                linhas.append(
                    f'<tr>{celula_tarefa(t)}'
                    f'{td(t["responsavel"]["nome"] or "—", cor="#555")}'
                    f'{td(fmt_d(t["d_limite_calc"]) if t["d_limite_calc"] else "—")}'
                    f'</tr>'
                )
        s2_deps_html = (
            '<div style="margin-bottom:14px;">'
            + sub_titulo("Com pendência", len(rel["deps_externas"]))
            + tabela_wrap(linhas, ["Tarefa", "Responsável", "Prazo"])
            + '</div>'
        )
    else:
        s2_deps_html = ""

    if rel["proj_sem_mov"]:
        linhas = []
        for p in rel["proj_sem_mov"]:
            cor_dias = "#e53e3e" if (p["dias"] or 0) >= 14 else "#c05621"
            dias_str = (str(p["dias"]) + "d") if p["dias"] is not None else "—"
            n_tf = len(p["tarefas"])
            statuses = ", ".join(sorted(set(t["status"] for t in p["tarefas"])))
            responsaveis = ", ".join(sorted(set(
                t["responsavel"]["nome"] for t in p["tarefas"] if t["responsavel"]["nome"]
            ))) or "—"
            tarefas_html = (
                f'{n_tf} tarefa(s)<br>'
                f'<span style="color:#bbb;font-size:11px;">{statuses}</span>'
            )
            linhas.append(
                f'<tr>'
                f'{td("<strong>" + p["nome"] + "</strong>", nowrap=False)}'
                f'{td(dias_str, cor=cor_dias, negrito=True)}'
                f'{td(fmt_d(p["ultima"]))}'
                f'{td(tarefas_html, nowrap=False)}'
                f'{td(responsaveis, cor="#888")}'
                f'</tr>'
            )
        s2_sem_mov = tabela_wrap(
            linhas, ["Projeto", "Parado há", "Última ativ.", "Tarefas abertas", "Responsável(is)"]
        )
    else:
        s2_sem_mov = vazio("Nenhum projeto sem movimentação há mais de 7 dias.")

    s2 = (
        '<div style="margin-bottom:26px;">'
        + secao_titulo("Seção 2 — Gargalos e Pendências")
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Aguardando Lícia", len(rel["aguarda_licia_rev"])) + s2_licia
        + '</div>'
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Aguardando Willian", willian_total) + s2_willian
        + '</div>'
        + s2_deps_html
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Projetos sem movimentação", len(rel["proj_sem_mov"])) + s2_sem_mov
        + '</div>'
        + '</div>'
    )

    # ── Seção 3 — Prazos e Próximas Entregas ─────────────────────────────────────

    proj_atrasados = OrderedDict()
    for t in rel["atrasadas"]:
        pn = t.get("projeto_nome") or "Sem projeto"
        proj_atrasados.setdefault(pn, []).append(t)

    proj_vencem7 = OrderedDict()
    for t in rel["vencem7"]:
        pn = t.get("projeto_nome") or "Sem projeto"
        if pn not in proj_atrasados:
            proj_vencem7.setdefault(pn, []).append(t)

    COLUNAS_S3 = ["Projeto", "Prazo", "Situação", "Pendências", "Responsável"]

    def proj_row_s3(proj_nome, tarefas_proj, modo):
        prazos = [t["d_limite_calc"] for t in tarefas_proj if t["d_limite_calc"]]
        prazo_min = min(prazos) if prazos else None
        responsaveis = ", ".join(sorted(set(
            t["responsavel"]["nome"] for t in tarefas_proj if t["responsavel"]["nome"]
        ))) or "—"
        n = len(tarefas_proj)
        status_counts = {}
        for t in tarefas_proj:
            s = t["status"]
            status_counts[s] = status_counts.get(s, 0) + 1
        status_str = ", ".join(
            f'{v} em {k}' for k, v in sorted(status_counts.items(), key=lambda x: -x[1])
        )
        pendencias_html = (
            f'{n} tarefa(s)<br>'
            f'<span style="font-size:11px;color:#bbb;">{status_str}</span>'
        )
        url = tarefas_proj[0]["url"]
        nome_link = (
            f'<a href="{url}" style="color:#1a1a1a;text-decoration:none;'
            f'font-weight:600;font-size:13px;">{proj_nome}</a>'
        )
        if modo == "atrasado":
            max_atraso = max((t.get("dias_atraso") or 0) for t in tarefas_proj)
            situacao_html = f'<span style="color:#e53e3e;font-weight:600;">Atrasado · {max_atraso}d</span>'
            prazo_html = fmt_d(prazo_min) if prazo_min else "—"
            cor_prazo = "#e53e3e"
        else:
            dias_rest = (prazo_min - hoje).days if prazo_min else None
            cor_prazo = "#c05621" if (dias_rest is not None and dias_rest <= 3) else "#555"
            prazo_html = fmt_d(prazo_min) if prazo_min else "—"
            situacao_html = (
                f'<span style="color:{cor_prazo};font-weight:600;">Em {dias_rest}d</span>'
                if dias_rest is not None
                else '<span style="color:#888;">Em andamento</span>'
            )
        return (
            f'<tr>'
            f'{td(nome_link, nowrap=False)}'
            f'{td(prazo_html, cor=cor_prazo)}'
            f'{td(situacao_html, nowrap=False)}'
            f'{td(pendencias_html, nowrap=False)}'
            f'{td(responsaveis, cor="#888")}'
            f'</tr>'
        )

    if proj_atrasados:
        linhas = [proj_row_s3(pn, ts, "atrasado") for pn, ts in proj_atrasados.items()]
        s3_atrasados = tabela_wrap(linhas, COLUNAS_S3)
    else:
        s3_atrasados = vazio(
            "Nenhum projeto com tarefas atrasadas. Preencha D. Início + Dias úteis para ativar esta seção."
        )

    if proj_vencem7:
        linhas = [proj_row_s3(pn, ts, "vencem") for pn, ts in proj_vencem7.items()]
        s3_vencem = tabela_wrap(linhas, COLUNAS_S3)
    else:
        s3_vencem = vazio("Nenhum projeto com entregas nos próximos 7 dias.")

    s3 = (
        '<div style="margin-bottom:26px;">'
        + secao_titulo("Seção 3 — Prazos e Próximas Entregas")
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Atrasados", len(proj_atrasados)) + s3_atrasados
        + '</div>'
        + '<div style="margin-bottom:14px;">'
        + sub_titulo("Próximos 7 dias", len(proj_vencem7)) + s3_vencem
        + '</div>'
        + '</div>'
    )

    # ── Seção 4 — Equipe e Produtividade ─────────────────────────────────────────

    def equipe_card(nome, tarefas_pessoa, aguarda_rev, vencem7_all):
        ids_pessoa = {t["id"] for t in tarefas_pessoa}
        n_total_p  = len(tarefas_pessoa)
        n_exec     = sum(1 for t in tarefas_pessoa if t["status"] != "Revisão")
        n_rev_acao = len(aguarda_rev)
        n_atras_p  = sum(1 for t in tarefas_pessoa if (t.get("dias_atraso") or 0) > 0)
        n_vence7_p = sum(1 for t in vencem7_all if t["id"] in ids_pessoa)

        def linha_stat(label, valor, cor="#1a1a1a", negrito=False):
            peso = "font-weight:600;" if negrito else ""
            return (
                f'<div style="display:flex;justify-content:space-between;padding:7px 0;'
                f'border-bottom:1px solid #f8f8f8;">'
                f'<span style="font-size:13px;color:#555;">{label}</span>'
                f'<span style="font-size:13px;color:{cor};{peso}">{valor}</span>'
                f'</div>'
            )

        alertas_p = []
        if n_atras_p > 0:
            alertas_p.append(alerta(f'{n_atras_p} tarefa(s) atrasada(s).', "red"))
        if n_rev_acao > 0:
            alertas_p.append(alerta(f'{n_rev_acao} revisão(ões) aguardando ação de {nome}.', "yellow"))
        alerta_html = "".join(alertas_p) if alertas_p else alerta(
            f'Carga controlada — {n_total_p} tarefa(s) ativa(s).', "green"
        )

        return (
            '<div style="flex:1;min-width:260px;border:1px solid #eee;border-radius:8px;'
            'padding:16px 18px;margin-bottom:14px;">'
            f'<p style="margin:0 0 12px;font-size:13px;font-weight:700;color:#1a1a1a;">{nome}</p>'
            + linha_stat("Total de tarefas", n_total_p)
            + linha_stat("Em execução", n_exec)
            + linha_stat(
                "Revisões aguardando ação", n_rev_acao,
                cor="#c05621" if n_rev_acao > 0 else "#1a1a1a",
                negrito=n_rev_acao > 0,
            )
            + linha_stat(
                "Tarefas atrasadas", n_atras_p,
                cor="#e53e3e" if n_atras_p > 0 else "#1a1a1a",
                negrito=n_atras_p > 0,
            )
            + linha_stat("Entregas nos próximos 7 dias", n_vence7_p)
            + f'<div style="margin-top:12px;">{alerta_html}</div>'
            + '</div>'
        )

    s4_licia   = equipe_card("Lícia",   rel["licia"],   rel["aguarda_licia_rev"],   rel["vencem7"])
    s4_willian = equipe_card("Willian", rel["willian"], rel["aguarda_willian_rev"], rel["vencem7"])

    s4 = (
        '<div style="margin-bottom:26px;">'
        + secao_titulo("Seção 4 — Equipe e Produtividade")
        + '<div style="display:flex;gap:16px;flex-wrap:wrap;">'
        + s4_licia + s4_willian
        + '</div></div>'
    )

    # ── Montagem final ──────────────────────────────────────────────────────────
    corpo = (
        s1 + divider()
        + s2 + divider()
        + s3 + divider()
        + s4
        + '<p style="text-align:right;font-size:11px;color:#ccc;margin:24px 0 0;">'
        + 'Gerado automaticamente · Simplific</p>'
    )
    return (
        '<!DOCTYPE html><html>'
        '<head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#f0f0f0;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;color:#1a1a1a;">'
        '<div style="max-width:720px;margin:32px auto;">'
        '<div style="background:#1a1a1a;padding:24px 28px;border-radius:10px 10px 0 0;">'
        '<p style="margin:0;color:#666;font-size:11px;text-transform:uppercase;letter-spacing:1.5px;">'
        'Relatório Diário de Produção · Simplific</p>'
        f'<p style="margin:6px 0 0;color:#fff;font-size:20px;font-weight:600;">{hoje_str}</p>'
        f'<p style="margin:4px 0 0;color:#888;font-size:12px;">'
        f'{rel["total"]} tarefas ativas · Gerado automaticamente</p>'
        '</div>'
        f'<div style="background:#fff;padding:28px;border-radius:0 0 10px 10px;">{corpo}</div>'
        '</div></body></html>'
    )


# ── Envio de e-mail ─────────────────────────────────────────────────────────────

def enviar(html, hoje):
    assunto = f"Relatório Diário de Produção | {hoje.strftime('%d/%m')}"
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

    ids_vistos = set()
    for t in tarefas:
        if t["projeto_nome"]:
            continue
        pid = t["projeto_id"]
        if not pid:
            continue
        if pid not in ids_vistos:
            ids_vistos.add(pid)
            t["projeto_nome"] = buscar_nome_projeto(pid)
        else:
            t["projeto_nome"] = _proj_cache.get(pid.replace("-", ""))
    if ids_vistos:
        print(f"{len(ids_vistos)} projetos carregados via API (fallback).")

    tarefas = [t for t in tarefas if not projeto_inativo(t["projeto_status"])]
    tarefas = [
        t for t in tarefas
        if (t["projeto_nome"] or "").strip().lower() != PROJETO_TEMPLATE
    ]
    print(f"{len(tarefas)} tarefas após filtros.")

    print("Montando relatório...")
    rel = montar(tarefas)

    print("Gerando HTML...")
    html = gerar_html(rel)

    print("Enviando e-mail...")
    enviar(html, rel["hoje"])
    print("✓ Relatório enviado com sucesso.")


if __name__ == "__main__":
    main()
