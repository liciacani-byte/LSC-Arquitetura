#!/usr/bin/env python3
"""
Simulação da seleção de prioridades com dados reais do Notion.
NÃO envia e-mail. Imprime resultado nos logs do GitHub Actions.

Executa o novo algoritmo (proposto) e compara com o atual.
"""

import os
import sys
import requests
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID  = "29bfab6becce8168830ae194246e85cb"

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

_proj_cache = {}

STATUS_PROJETO_INATIVO = {"finalizado", "cancelado", "arquivado", "concluído", "concluido", "suspenso"}
PROJETO_TEMPLATE = "projeto base"
KEYWORDS_DEP_EXTERNA = {"cliente", "fornecedor", "prefeitura", "topografia", "laudo", "externo", "terceiro"}


# ── API ──────────────────────────────────────────────────────────────────────────

def api_get(path):
    r = requests.get(f"https://api.notion.com/v1/{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()

def api_post(path, payload):
    r = requests.post(f"https://api.notion.com/v1/{path}", headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


# ── Busca ────────────────────────────────────────────────────────────────────────

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
    try:
        data = api_get("users")
        return {u["id"]: u.get("name", "") for u in data.get("results", [])}
    except Exception:
        return {}

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


# ── Extração de campos (idêntica ao relatorio.py) ────────────────────────────────

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
    return d.strftime("%d/%m") if d else "—"

def extrair(t, usuarios):
    props = t.get("properties", {})

    def texto(campo):
        v = props.get(campo, {})
        tp = v.get("type")
        if tp == "title":
            return "".join(x["plain_text"] for x in v.get("title", []))
        if tp == "rich_text":
            return "".join(x["plain_text"] for x in v.get("rich_text", []))
        if tp in ("select", "status"):
            val = v.get(tp) or {}
            return val.get("name")
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
        for campo in ("Projeto", "2026 |  Projetos ", "2026 | Projetos", "Projetos", "projeto", "Project"):
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
        return None

    def ler_status_projeto():
        for campo in ("Status do Projeto", "Status Projeto", "Status do projeto", "status do projeto", "Project Status"):
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
        return None

    l1 = checkbox("1° | Lícia ")
    w1 = checkbox("1° | Willian")
    l2 = checkbox("2° | Lícia")
    w2 = checkbox("2° | Willian")

    return {
        "id":            t["id"],
        "nome":          texto("Tarefa / Projeto") or "(sem nome)",
        "status":        texto("Status da Tarefa") or "",
        "etapa":         texto("Etapa") or "",
        "observacao":    texto("Observação") or "",
        "d_inicio":      data_campo("D. Início"),
        "dias_uteis":    numero("Dias úteis"),
        "feriados":      numero("Feriados") or 0,
        "responsavel":   responsavel(),
        "ultima_edicao": data_campo("Última edição") or t.get("last_edited_time"),
        "rev_licia1":    l1,
        "rev_willian1":  w1,
        "rev_licia2":    l2,
        "rev_willian2":  w2,
        "projeto_id":    projeto_id(),
        "projeto_nome":  ler_projeto(),
        "projeto_status": ler_status_projeto(),
        "url":           t.get("url", ""),
        "d_limite_calc": None,
        "dias_atraso":   None,
    }

def info_revisao_aguarda_licia(t):
    l1, w1, l2, w2 = t["rev_licia1"], t["rev_willian1"], t["rev_licia2"], t["rev_willian2"]
    if w2:
        return True   # aguarda Lícia validar
    if l2:
        return False  # aguarda Willian
    if w1:
        return True   # aguarda 2ª revisão Lícia
    if l1:
        return False  # aguarda Willian 1ª correção
    return True       # aguarda 1ª revisão Lícia

def projeto_inativo(status_str):
    if not status_str:
        return False
    return any(p in status_str.lower() for p in STATUS_PROJETO_INATIVO)


# ── Montagem dos dados ────────────────────────────────────────────────────────────

def montar(tarefas):
    hoje = datetime.utcnow().date()
    em7  = hoje + timedelta(days=7)
    ha7  = hoje - timedelta(days=7)

    atrasadas, vencem7 = [], []
    aguarda_licia_rev, aguarda_willian_rev = [], []

    for t in tarefas:
        prazo = calcular_prazo(t["d_inicio"], t["dias_uteis"], t["feriados"])
        t["d_limite_calc"] = prazo
        if prazo:
            if prazo < hoje:
                t["dias_atraso"] = (hoje - prazo).days
                atrasadas.append(t)
            elif prazo <= em7:
                vencem7.append(t)

        if t["status"] == "Revisão":
            if info_revisao_aguarda_licia(t):
                aguarda_licia_rev.append(t)
            else:
                aguarda_willian_rev.append(t)

    return {
        "hoje":               hoje,
        "total":              len(tarefas),
        "atrasadas":          sorted(atrasadas, key=lambda x: -(x["dias_atraso"] or 0)),
        "vencem7":            sorted(vencem7, key=lambda x: x["d_limite_calc"]),
        "aguarda_licia_rev":  aguarda_licia_rev,
        "aguarda_willian_rev": aguarda_willian_rev,
    }


# ── Novo algoritmo (proposto) ─────────────────────────────────────────────────────

def selecionar_prioridades_novo(rel):
    hoje = rel["hoje"]
    projetos = {}

    def get_proj(nome):
        if nome not in projetos:
            projetos[nome] = {
                "nome": nome,
                "atrasadas": [],
                "vencem7": [],
                "aguarda_licia": [],
                "aguarda_willian": [],
                "representante": None,
            }
        return projetos[nome]

    for t in rel["atrasadas"]:
        pn = t.get("projeto_nome") or "Sem projeto"
        p = get_proj(pn)
        p["atrasadas"].append(t)
        if not p["representante"]:
            p["representante"] = t

    for t in rel["vencem7"]:
        pn = t.get("projeto_nome") or "Sem projeto"
        p = get_proj(pn)
        p["vencem7"].append(t)
        if not p["representante"]:
            p["representante"] = t

    for t in rel["aguarda_licia_rev"]:
        pn = t.get("projeto_nome") or "Sem projeto"
        p = get_proj(pn)
        p["aguarda_licia"].append(t)
        if not p["representante"]:
            p["representante"] = t

    for t in rel["aguarda_willian_rev"]:
        pn = t.get("projeto_nome") or "Sem projeto"
        p = get_proj(pn)
        p["aguarda_willian"].append(t)
        if not p["representante"]:
            p["representante"] = t

    candidatos = []

    for pn, p in projetos.items():
        if not (p["atrasadas"] or p["vencem7"] or p["aguarda_licia"] or p["aguarda_willian"]):
            continue

        peso = 0
        tipo_principal = None

        # Atrasadas: tempo + volume (ambos limitados para não dominar sozinhos)
        if p["atrasadas"]:
            max_atraso = max((t.get("dias_atraso") or 0) for t in p["atrasadas"])
            n = len(p["atrasadas"])
            peso_a = 100 + min(max_atraso, 30) + min(n * 3, 15)
            if peso_a > peso:
                peso = peso_a
                tipo_principal = "atrasada"

        # Vencem em 7 dias: proximidade + volume
        if p["vencem7"]:
            prazos = [t["d_limite_calc"] for t in p["vencem7"] if t["d_limite_calc"]]
            prazo_min = min(prazos) if prazos else None
            dias_rest = (prazo_min - hoje).days if prazo_min else 7
            n = len(p["vencem7"])
            peso_v = 80 + (7 - dias_rest) * 2 + min(n * 2, 6)
            if peso_v > peso:
                peso = peso_v
                tipo_principal = tipo_principal or "prazo"

        # Revisões bloqueantes (sem prazo): peso limitado, nunca supera deadline
        # Só entram se não houver atrasadas nem vencem7 neste projeto
        if not p["atrasadas"] and not p["vencem7"]:
            if p["aguarda_licia"]:
                n = len(p["aguarda_licia"])
                peso_r = 40 + min(n * 2, 8)  # máx 48
                if peso_r > peso:
                    peso = peso_r
                    tipo_principal = "revisao_licia"
            elif p["aguarda_willian"]:
                n = len(p["aguarda_willian"])
                peso_r = 35 + min(n * 2, 8)  # máx 43
                if peso_r > peso:
                    peso = peso_r
                    tipo_principal = "revisao_willian"

        if tipo_principal:
            candidatos.append({
                "peso": peso,
                "tipo": tipo_principal,
                "projeto": pn,
                "proj_data": p,
                "representante": p["representante"],
            })

    candidatos.sort(key=lambda x: -x["peso"])

    # Filtro de relevância: revisões sem prazo só entram se peso > 42
    resultado = []
    for c in candidatos:
        tem_prazo = bool(c["proj_data"]["atrasadas"] or c["proj_data"]["vencem7"])
        if tem_prazo or c["peso"] > 42:
            resultado.append(c)
        if len(resultado) == 5:
            break

    return resultado


# ── Algoritmo atual (para comparação) ────────────────────────────────────────────

def selecionar_prioridades_atual(rel):
    hoje = rel["hoje"]
    candidatos = []
    vistos = set()

    def add(peso, tipo, tarefa, resumo, acao):
        if tarefa["id"] not in vistos:
            candidatos.append({"peso": peso, "tipo": tipo, "tarefa": tarefa, "resumo": resumo, "acao": acao})
            vistos.add(tarefa["id"])

    for t in rel["atrasadas"]:
        add(100 + (t.get("dias_atraso") or 0), "atrasada",
            t, f'Atrasada há {t["dias_atraso"]}d', t["responsavel"]["nome"] or "—")

    for t in rel["vencem7"]:
        if t["d_limite_calc"]:
            dias_rest = (t["d_limite_calc"] - hoje).days
            add(90 - dias_rest, "prazo",
                t, f'Prazo em {fmt_d(t["d_limite_calc"])} ({dias_rest}d)', t["responsavel"]["nome"] or "—")

    for t in rel["aguarda_licia_rev"]:
        ultima = parse_d(t["ultima_edicao"])
        dias_esp = (hoje - ultima).days if ultima else 0
        add(60 + dias_esp, "revisao_licia", t, "Aguardando revisão de Lícia", "Lícia")

    for t in rel["aguarda_willian_rev"]:
        ultima = parse_d(t["ultima_edicao"])
        dias_esp = (hoje - ultima).days if ultima else 0
        add(55 + dias_esp, "revisao_willian", t, "Aguardando correção de Willian", "Willian")

    candidatos.sort(key=lambda x: -x["peso"])
    return candidatos[:5]


# ── Formatação do resultado ───────────────────────────────────────────────────────

LABEL_TIPO = {
    "atrasada":        "ATRASADA",
    "prazo":           "PRAZO PRÓXIMO",
    "revisao_licia":   "AGUARDA LÍCIA",
    "revisao_willian": "AGUARDA WILLIAN",
}

def proxima_acao(p):
    pd = p["proj_data"]
    if pd["aguarda_licia"]:
        bloqueio = f"liberar {len(pd['aguarda_licia'])} tarefa(s) de Willian" if pd["aguarda_willian"] else "prosseguir"
        return f"Lícia revisar para {bloqueio}"
    if pd["aguarda_willian"]:
        return f"Willian corrigir {len(pd['aguarda_willian'])} tarefa(s)"
    if pd["atrasadas"]:
        resps = sorted(set(t["responsavel"]["nome"] for t in pd["atrasadas"] if t["responsavel"]["nome"]))
        return f"{' / '.join(resps) or '—'} retomar execução"
    if pd["vencem7"]:
        resps = sorted(set(t["responsavel"]["nome"] for t in pd["vencem7"] if t["responsavel"]["nome"]))
        return f"{' / '.join(resps) or '—'} priorizar entrega"
    return "—"

def resumir_projeto(p, hoje):
    pd = p["proj_data"]
    linhas = []

    if pd["atrasadas"]:
        max_at = max((t.get("dias_atraso") or 0) for t in pd["atrasadas"])
        linhas.append(f"  • {len(pd['atrasadas'])} tarefa(s) atrasada(s) · há {max_at}d")

    if pd["vencem7"]:
        prazos = [t["d_limite_calc"] for t in pd["vencem7"] if t["d_limite_calc"]]
        prazo_min = min(prazos) if prazos else None
        dias_rest = (prazo_min - hoje).days if prazo_min else None
        suffix = f" · {dias_rest}d para entrega" if dias_rest is not None else ""
        linhas.append(f"  • {len(pd['vencem7'])} tarefa(s) com prazo próximo{suffix}")

    # Revisões: só mostrar o que não já está contado em atrasadas/vencem7
    licia_ids  = {t["id"] for t in pd["aguarda_licia"]}
    willian_ids = {t["id"] for t in pd["aguarda_willian"]}
    at_ids     = {t["id"] for t in pd["atrasadas"]}
    v7_ids     = {t["id"] for t in pd["vencem7"]}

    licia_extras   = licia_ids - at_ids - v7_ids
    willian_extras = willian_ids - at_ids - v7_ids

    if licia_extras:
        linhas.append(f"  • {len(licia_extras)} revisão(ões) aguardando Lícia")
    if willian_extras:
        linhas.append(f"  • {len(willian_extras)} correção(ões) aguardando Willian")

    linhas.append(f"  → Próxima ação: {proxima_acao(p)}")
    return "\n".join(linhas)


def imprimir_resultado(titulo, prioridades, rel):
    print(f"\n{'='*60}")
    print(f"  {titulo}")
    print(f"{'='*60}")
    if not prioridades:
        print("  ✅ Nenhuma prioridade crítica identificada.")
        return

    for i, p in enumerate(prioridades, 1):
        if "proj_data" in p:
            # Novo algoritmo
            tipo_label = LABEL_TIPO.get(p["tipo"], p["tipo"].upper())
            print(f"\n  [{i}] {p['projeto']}  [{tipo_label}]  (peso: {p['peso']})")
            print(resumir_projeto(p, rel["hoje"]))
        else:
            # Algoritmo atual
            t = p["tarefa"]
            proj = t.get("projeto_nome") or "Sem projeto"
            tipo_label = LABEL_TIPO.get(p["tipo"], p["tipo"].upper())
            print(f"\n  [{i}] {t['nome']}")
            print(f"       Projeto: {proj}  [{tipo_label}]  (peso: {p['peso']})")
            print(f"       {p['resumo']} · Ação: {p['acao']}")


# ── Main ──────────────────────────────────────────────────────────────────────────

def main():
    print("Buscando usuários...")
    usuarios = buscar_usuarios()

    print("Buscando tarefas...")
    raw = buscar_tarefas()
    print(f"{len(raw)} tarefas encontradas.")

    print("Extraindo campos...")
    tarefas = [extrair(t, usuarios) for t in raw]

    # Fallback para nomes de projeto via API
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

    # Filtros
    tarefas = [t for t in tarefas if not projeto_inativo(t["projeto_status"])]
    tarefas = [t for t in tarefas if (t["projeto_nome"] or "").strip().lower() != PROJETO_TEMPLATE]
    print(f"{len(tarefas)} tarefas após filtros.")

    rel = montar(tarefas)

    print(f"\nData de referência: {rel['hoje']}")
    print(f"Total ativo: {rel['total']}")
    print(f"Atrasadas: {len(rel['atrasadas'])}")
    print(f"Vencem em 7 dias: {len(rel['vencem7'])}")
    print(f"Revisões aguardando Lícia: {len(rel['aguarda_licia_rev'])}")
    print(f"Revisões aguardando Willian: {len(rel['aguarda_willian_rev'])}")

    # Comparação dos dois algoritmos
    atual = selecionar_prioridades_atual(rel)
    novo  = selecionar_prioridades_novo(rel)

    imprimir_resultado("ALGORITMO ATUAL (em produção)", atual, rel)
    imprimir_resultado("ALGORITMO PROPOSTO (simulação)", novo, rel)

    print(f"\n{'='*60}")
    print(f"  Projetos únicos no novo algoritmo: {len(set(p['projeto'] for p in novo))}")
    print(f"  Projetos únicos no atual:          {len(set((p['tarefa'].get('projeto_nome') or 'Sem projeto') for p in atual))}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
