"""
Referência de horas por atividade — LSC Arquitetura

Lê TODOS os registros da base "Controle de Horas LSC" no Notion e monta a média
geral acumulada de horas por entrega, organizada na estrutura de etapas do
escritório (Geral, Projeto Arquitetônico, Projeto Executivo, Projeto de
Interiores por ambiente) e separada por profissional (Lícia e Willian), para usar
em orçamentos. A cada execução o cálculo é refeito do zero com todo o histórico,
então as entregas concluídas entram automaticamente no acumulado.

Uma "entrega" é a soma de todas as horas de um item da estrutura dentro de um
projeto, separada por profissional. Ex.: toda a Planta de Pontos do Willian no
projeto X = 1 entrega dele.

As médias de Lícia e Willian são calculadas cada uma sobre as entregas em que
aquela pessoa trabalhou, e nunca são somadas entre si.

Uso:
    python relatorio_mensal_horas.py                     # até o fim do mês anterior, envia e-mail
    python relatorio_mensal_horas.py --ate 2026-10       # até o fim de outubro de 2026
    python relatorio_mensal_horas.py --salvar teste.html # gera o HTML (e o CSV ao lado) sem enviar

Variáveis de ambiente:
    NOTION_TOKEN        token da integração do Notion
    GMAIL_USER          conta Gmail que envia (ex.: liciacani@gmail.com)
    GMAIL_APP_PASSWORD  senha de app do Gmail (não é a senha normal)
    EMAIL_DESTINO       destinatários, separados por vírgula

Usa os mesmos secrets do relatorio.py e do relatorio_semanal.py, que não são alterados.
"""

import argparse
import csv
import io
import json
import os
import re
import smtplib
import statistics
import sys
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---------------------------------------------------------------- configuração

HORAS_DB_ID = "137fab6becce8004bbcde641510baffd"  # Controle de Horas LSC (mesmo do relatorio_semanal.py)
NOTION_VERSION = "2022-06-28"
FUSO = timezone(timedelta(hours=-3))  # Brasília (sem horário de verão desde 2019)

# Profissional = página relacionada em "Ref. Equipe" (id da página -> nome).
# É a mesma relação que traz o custo-hora usado na fórmula "Custo".
PROFISSIONAIS = {
    "15cfab6becce8026af9ee0407adc0bcb": "Lícia",
    "15cfab6becce802abbf2c08bae46e19d": "Willian",
}
# Usuário do Notion -> página da Ref. Equipe (só para conferir quem lançou).
USUARIOS = {
    "8d1f8b43-3f9e-43b7-939f-d432f7a630d1": "15cfab6becce8026af9ee0407adc0bcb",
    "6550ee09-a47c-47ee-ace3-a14b055cbb8b": "15cfab6becce802abbf2c08bae46e19d",
}

# Campo Etapa do registro -> seção do relatório. Registros de outras etapas
# (Gestão, Assessoria, Simplific) não entram.
ARQ, EXE, INT, GERAL = "Projeto Arquitetônico", "Projeto Executivo", "Projeto de Interiores", "Geral"
ETAPAS = {
    "I - Projeto Arquitetônico": ARQ,
    "II - Projeto Executivo": EXE,
    "III - Projeto de Interiores": INT,
}

# ---------------------------------------------------------------- estrutura do relatório

# Itens fixos de cada seção, na ordem em que aparecem no e-mail.
PLANTA_AMBIENTADA = "Desenvolvimento da Planta Baixa Ambientada"
REUNIAO_AMBIENTACAO = "Reunião para apresentação da ambientação + Ata"
REVISAO = "Revisão e Ajustes"
CORRECAO = "Correção"
REUNIAO_ENTREGA = "Reunião de Entrega"

ESTRUTURA = {
    GERAL: ["Reunião de Briefing", "Visita para Levantamento de Dados"],
    ARQ: [PLANTA_AMBIENTADA, REUNIAO_AMBIENTACAO, REVISAO, "3D | Fachada",
          "Detalhamento do Projeto Arquitetônico", CORRECAO, "Compatibilização com Estrutural", REUNIAO_ENTREGA],
    EXE: [PLANTA_AMBIENTADA, REUNIAO_AMBIENTACAO, REVISAO, "Planta de Reforma", "Planta de Pontos",
          "Planta de Iluminação", "Planta de Gesso", "Paginação de Piso", "Detalhamento de Portas",
          "Detalhamento de Vidros", "Detalhamento de Cortinas", "Muro e Calçada", "Planta de Pintura",
          "Memorial de Iluminação e Pintura", CORRECAO, REUNIAO_ENTREGA],
}
# Em Interiores, cada ambiente tem estes itens.
ATIVIDADES_AMBIENTE = ["Criação", "Detalhamento de obra", "Detalhamento de marcenaria", "Correção"]
SEM_AMBIENTE = "Ambiente não informado"
SEM_TIPO_AMBIENTE = "Detalhamento sem tipo (obra ou marcenaria)"

# Ambientes reconhecidos na Descrição (primeiro que casar vence; padrões sem acento).
AMBIENTES = [
    ("Varanda", r"varanda"),
    ("Cozinha Gourmet", r"gourmet|churrasqueira"),
    ("Cozinha", r"cozinha"),
    ("Copa", r"\bcopa\b"),
    ("Despensa", r"despensa"),
    ("Área de Serviço", r"servico|\bserv\b"),
    ("Lavabo", r"lavabo"),
    ("Banheiro Suíte", r"banheiro suite"),
    ("Banheiro Social", r"banheiro social"),
    ("Banheiros", r"banheiro"),
    ("Quarto Casal", r"quarto casal|\bq\.? casal"),
    ("Quarto Bebê", r"bebe"),
    ("Quarto Filho(a)", r"quarto filh"),
    ("Quarto Hóspedes", r"hospede"),
    ("Suíte", r"suite"),
    ("Closet", r"closet"),
    ("Sala de Jantar", r"jantar"),
    ("Sala de TV / Estar", r"sala de tv|\bestar\b"),
    ("Outras salas", r"\bsala"),
    ("Escritório", r"escritorio"),
    ("Brinquedoteca", r"brinquedoteca"),
    ("Circulação", r"circulacao|\bhall\b"),
    ("Área de Lazer / Piscina", r"lazer|deck|piscina"),
    ("Depósito", r"deposi"),
    ("Capela / Oratório", r"capela|oratorio"),
    ("Fachada", r"fachada"),
]

# Linhas que aparecem quando há horas que não cabem em nenhum item da estrutura
# (ficam separadas para não distorcer as médias dos itens).
FORA_ESTRUTURA = {
    "reuniao": "Outras reuniões (tipo não informado)",
    "executivo": "Projeto Executivo (descrição genérica)",
    "3d": "3D",
    "compatibilizacao": "Compatibilização",
    "outros_detalhes": "Outros detalhamentos (fachada, serralheria, escada, obra)",
    "memoriais": "Memoriais",
    "cortinas": "Cortinas",
    "entrega": REUNIAO_ENTREGA,
}

# Uma entrega só entra na média quando a etapa daquele projeto terminou: o projeto
# foi finalizado, ou a seção inteira ficou esse tempo sem nenhum lançamento.
# Projetos cancelados ficam de fora (o trabalho foi interrompido no meio).
STATUS_PROJETO_CONCLUIDO = {"Finalizado", "Projeto Finalizado | Execução"}
STATUS_PROJETO_CANCELADO = {"Cancelado"}
DIAS_SEM_LANCAMENTO = 30

# Registros acima disso são tratados como erro de digitação (ex.: Fim no dia errado).
DURACAO_MAXIMA_HORAS = 12


# ---------------------------------------------------------------- utilidades

def sem_acento(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c)).lower().strip()


def tem(padrao, texto):
    return re.search(padrao, texto) is not None


def ambiente_de(texto):
    for nome, padrao in AMBIENTES:
        if tem(padrao, texto):
            return nome
    return None


def classificar(etapa, atividade, descricao):
    """
    Devolve (seção, ambiente, item) ou None quando não dá para classificar.
    ambiente só é usado em Interiores; nas outras seções é None.
    O campo Atividade, se um dia for preenchido, é lido junto com a Descrição.
    """
    secao = ETAPAS.get(etapa)
    if not secao:
        return None
    t = sem_acento(f"{atividade or ''} {descricao or ''}")
    if not t:
        return None

    # Geral: vale para qualquer etapa.
    if tem(r"briefing", t):
        return GERAL, None, "Reunião de Briefing"
    if tem(r"levantamento|\bld\b|conferir medida|medicao", t):
        return GERAL, None, "Visita para Levantamento de Dados"

    if secao == INT:
        return classificar_interiores(t)

    if tem(r"compatibiliz", t):
        return secao, None, "Compatibilização com Estrutural" if secao == ARQ else FORA_ESTRUTURA["compatibilizacao"]

    # Reuniões e apresentações (Arquitetônico e Executivo).
    if tem(r"entrega|termo", t):
        return secao, None, REUNIAO_ENTREGA
    if tem(r"reuniao|apresentac", t):
        if tem(r"apresentac|ambienta|\bata\b", t):
            return secao, None, REUNIAO_AMBIENTACAO
        return secao, None, FORA_ESTRUTURA["reuniao"]

    # Retrabalho: correção é diferente de revisão/ajuste da ambientação.
    if tem(r"correc", t):
        return secao, None, CORRECAO
    if tem(r"revisao|ajuste|alterac", t):
        return secao, None, REVISAO

    if tem(r"ambienta|planta baixa|layout|^criacao|estudo", t):
        return secao, None, PLANTA_AMBIENTADA

    if secao == ARQ:
        if tem(r"estrutur", t):
            return ARQ, None, "Compatibilização com Estrutural"
        if tem(r"\b3d\b|render", t):
            return ARQ, None, "3D | Fachada"
        if tem(r"arquitetonic|fachada|reforma|ampliac|uso misto|legal|aprovac|^pe\b|executivo", t):
            return ARQ, None, "Detalhamento do Projeto Arquitetônico"
        return None

    # Executivo
    if tem(r"memoria", t):
        return EXE, None, "Memorial de Iluminação e Pintura"
    if tem(r"pintura", t):
        return EXE, None, "Planta de Pintura"
    if tem(r"muro|calcada|portao|portoes", t):
        return EXE, None, "Muro e Calçada"
    if tem(r"reforma|ampliac|demolic", t):
        return EXE, None, "Planta de Reforma"
    if tem(r"pontos|eletric|hidraul", t):
        return EXE, None, "Planta de Pontos"
    if tem(r"iluminac", t):
        return EXE, None, "Planta de Iluminação"
    if tem(r"gesso|forro", t):
        return EXE, None, "Planta de Gesso"
    if tem(r"piso|paginac", t):
        return EXE, None, "Paginação de Piso"
    if tem(r"porta", t):
        return EXE, None, "Detalhamento de Portas"
    if tem(r"esquadria|vidro|janela", t):
        return EXE, None, "Detalhamento de Vidros"
    if tem(r"cortina", t):
        return EXE, None, "Detalhamento de Cortinas"
    if tem(r"executivo|^pe\b", t):
        return EXE, None, FORA_ESTRUTURA["executivo"]
    if tem(r"\b3d\b|render", t):
        return EXE, None, FORA_ESTRUTURA["3d"]
    if tem(r"fachada|serralheria|escada|pergolado|obra|marcenaria", t):
        return EXE, None, FORA_ESTRUTURA["outros_detalhes"]
    return None


def classificar_interiores(t):
    ambiente = ambiente_de(t)
    if tem(r"correc|revisao|ajuste|alterac", t):
        item = "Correção"
    elif tem(r"marcenaria", t):
        item = "Detalhamento de marcenaria"
    elif tem(r"det(\.|alhamento)? de obra|\(obra\)|\bobra\b", t) and not tem(r"visita|relatorio", t):
        item = "Detalhamento de obra"
    elif tem(r"\b3d\b|render|criac|ambienta|apresentac|modelagem", t):
        item = "Criação"
    elif tem(r"entrega|termo", t):
        return INT, None, FORA_ESTRUTURA["entrega"]
    elif tem(r"reuniao", t):
        return INT, None, FORA_ESTRUTURA["reuniao"]
    elif tem(r"memoria", t):
        return INT, None, FORA_ESTRUTURA["memoriais"]
    elif tem(r"cortina", t):
        return INT, None, FORA_ESTRUTURA["cortinas"]
    elif ambiente:
        item = SEM_TIPO_AMBIENTE  # só o nome do ambiente, sem dizer se é obra ou marcenaria
    else:
        return None
    return INT, ambiente or SEM_AMBIENTE, item


def parse_iso(valor):
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


def horas(minutos):
    if minutos is None:
        return "—"
    minutos = int(round(minutos))
    return f"{minutos // 60}h{minutos % 60:02d}"


def decimal_br(valor, casas=1):
    return "" if valor is None else f"{valor:.{casas}f}".replace(".", ",")


MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]


# ---------------------------------------------------------------- Notion

def notion_request(caminho, corpo):
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{caminho}",
        data=json.dumps(corpo).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def texto_de(prop):
    if not prop:
        return ""
    if prop["type"] == "title":
        return "".join(t["plain_text"] for t in prop["title"])
    if prop["type"] == "select":
        return (prop["select"] or {}).get("name", "")
    return ""


def data_com_hora(prop):
    valor = (prop or {}).get("date")
    if not valor or not valor.get("start") or "T" not in valor["start"]:
        return None
    return valor["start"]


def numero_formula(prop):
    formula = (prop or {}).get("formula") or {}
    return formula.get("number") if formula.get("type") == "number" else None


def numero_rollup(prop):
    rollup = (prop or {}).get("rollup") or {}
    return rollup.get("number") if rollup.get("type") == "number" else None


def custo_do_registro(p):
    """
    Fórmula "Custo" da base. Quando ela vem zerada, usa minutos x (V.H. Equipe +
    V.H Escritório), a mesma regra do relatorio_semanal.py.
    """
    custo = numero_formula(p.get("Custo"))
    if custo:
        return custo
    minutos = numero_formula(p.get("Minutos"))
    vh = (numero_rollup(p.get("V.H. Equipe")) or 0) + (numero_rollup(p.get("V.H Escritório")) or 0)
    if minutos and vh:
        return minutos / 60 * vh
    return custo


def status_rollup(prop):
    rollup = (prop or {}).get("rollup") or {}
    for item in rollup.get("array", []):
        status = item.get("status") or item.get("select")
        if status:
            return status.get("name")
    return None


def ids_relacao(prop):
    return [r["id"].replace("-", "") for r in (prop or {}).get("relation", [])]


def buscar_registros():
    registros, cursor = [], None
    while True:
        corpo = {"page_size": 100}
        if cursor:
            corpo["start_cursor"] = cursor
        resp = notion_request(f"databases/{HORAS_DB_ID}/query", corpo)
        for pagina in resp["results"]:
            p = pagina["properties"]
            projetos = ids_relacao(p.get("2026 | Projetos"))
            registros.append({
                "url": pagina["url"],
                "criado_por": (p.get("Criado por") or {}).get("created_by", {}).get("id"),
                "equipe": ids_relacao(p.get("Ref. Equipe")),
                "projeto": projetos[0] if projetos else None,
                "status_projeto": status_rollup(p.get("Status do Projeto")),
                "etapa": texto_de(p.get("Etapa")),
                "descricao": texto_de(p.get("Descrição")),
                "atividade": texto_de(p.get("Atividade")),
                "inicio": data_com_hora(p.get("Inicio")),
                "fim": data_com_hora(p.get("Fim")),
                "custo": custo_do_registro(p),
            })
        if not resp.get("has_more"):
            return registros
        cursor = resp["next_cursor"]


# ---------------------------------------------------------------- validação

def validar(registros, corte):
    """
    Devolve (sessões válidas, alertas).

    Cada sessão válida tem os minutos que realmente podem ser contados: registros
    duplicados são ignorados e, quando dois registros da mesma pessoa se
    sobrepõem, o trecho repetido só é contado uma vez. O custo é ajustado na
    mesma proporção dos minutos.

    Alerta = (profissional, descrição, início, problema, url, efeito, etapa),
    com efeito "fora" (não contado), "parcial" ou "contado" (só conferir).
    """
    alertas = []
    por_pessoa = defaultdict(list)

    def alerta(pessoa, r, motivo, efeito):
        alertas.append((pessoa, r["descricao"] or "(sem descrição)", r["inicio"], motivo, r["url"], efeito,
                        r.get("etapa")))

    for r in registros:
        if r["inicio"] and parse_iso(r["inicio"]) >= corte:
            continue
        equipe = [e for e in r["equipe"] if e in PROFISSIONAIS]
        if len(equipe) != 1:
            alerta("—", r, "Ref. Equipe vazia ou com mais de uma pessoa", "fora")
            continue
        pessoa_id = equipe[0]
        pessoa = PROFISSIONAIS[pessoa_id]

        if not r["inicio"] or not r["fim"]:
            alerta(pessoa, r, "sem horário de início ou de fim", "fora")
            continue
        inicio, fim = parse_iso(r["inicio"]), parse_iso(r["fim"])
        minutos = (fim - inicio).total_seconds() / 60
        if minutos <= 0:
            alerta(pessoa, r, "fim antes do início", "fora")
            continue
        if minutos > DURACAO_MAXIMA_HORAS * 60:
            alerta(pessoa, r, f"{horas(minutos)} seguidas (fim provavelmente no dia errado)", "fora")
            continue
        if r["criado_por"] and USUARIOS.get(r["criado_por"]) not in (None, pessoa_id):
            alerta(pessoa, r, f"lançado por outra pessoa — conferir se as horas são de {pessoa}", "contado")

        por_pessoa[pessoa].append({**r, "pessoa": pessoa, "ini": inicio, "fim_dt": fim, "min": minutos})

    sessoes = []
    for pessoa, lista in por_pessoa.items():
        lista.sort(key=lambda s: (s["ini"], s["fim_dt"]))
        vistos = set()
        maior_fim, dono_maior_fim = None, None
        for s in lista:
            chave = (s["ini"], s["fim_dt"])
            if chave in vistos:
                alerta(pessoa, s, "duplicado (mesmo início e fim de outro registro)", "fora")
                continue
            vistos.add(chave)

            contar_de = s["ini"]
            if maior_fim and s["ini"] < maior_fim:
                contar_de = min(maior_fim, s["fim_dt"])
                repetido = (contar_de - s["ini"]).total_seconds() / 60
                alerta(pessoa, s, f"sobreposto a “{dono_maior_fim}” — {horas(repetido)} contadas uma vez só",
                       "parcial")
            minutos = (s["fim_dt"] - contar_de).total_seconds() / 60
            if not maior_fim or s["fim_dt"] > maior_fim:
                maior_fim, dono_maior_fim = s["fim_dt"], s["descricao"] or "(sem descrição)"
            if minutos <= 0:
                continue
            custo = s["custo"] * minutos / s["min"] if s["custo"] is not None else None
            sessoes.append({**s, "min": minutos, "custo": custo,
                            "classe": classificar(s.get("etapa"), s["atividade"], s["descricao"])})

    alertas.sort(key=lambda a: a[2] or "", reverse=True)
    return sessoes, alertas


# ---------------------------------------------------------------- cálculo

def entregas_concluidas(sessoes, corte):
    """
    Agrupa as sessões em entregas (projeto + seção + ambiente + item + profissional)
    e devolve só as de seções concluídas até a data de corte.

    A conclusão é decidida pela seção inteira daquele projeto (ex.: todo o
    Executivo do projeto X), não por item: assim um item que só ficou parado
    enquanto o resto da etapa continua não entra na média pela metade.
    """
    grupos = defaultdict(lambda: {"min": 0.0, "custo": 0.0, "sem_custo": False})
    secao_info = defaultdict(lambda: {"ultimo": None, "status": None})
    for s in sessoes:
        if not s["projeto"] or not s["classe"]:
            continue
        secao, ambiente, item = s["classe"]
        g = grupos[(s["projeto"], secao, ambiente, item, s["pessoa"])]
        g["min"] += s["min"]
        if s["custo"] is None:
            g["sem_custo"] = True
        else:
            g["custo"] += s["custo"]
        info = secao_info[(s["projeto"], secao)]
        info["ultimo"] = max(info["ultimo"] or s["fim_dt"], s["fim_dt"])
        info["status"] = info["status"] or s["status_projeto"]

    limite = corte - timedelta(days=DIAS_SEM_LANCAMENTO)
    concluidas = {}
    for (projeto, secao, ambiente, item, pessoa), g in grupos.items():
        info = secao_info[(projeto, secao)]
        if info["status"] in STATUS_PROJETO_CANCELADO:
            continue
        if info["status"] in STATUS_PROJETO_CONCLUIDO or info["ultimo"] <= limite:
            concluidas[(projeto, secao, ambiente, item, pessoa)] = {
                "min": g["min"], "custo": None if g["sem_custo"] else g["custo"]}
    return concluidas


def calcular(entregas):
    """
    Devolve stats[seção][(ambiente, item)] com:
      "entregas": nº de projetos com aquele item concluído
      pessoa -> estatísticas só das entregas daquela pessoa
    """
    minutos = defaultdict(list)
    custos = defaultdict(list)
    projetos = defaultdict(set)
    for (projeto, secao, ambiente, item, pessoa), e in entregas.items():
        minutos[(secao, ambiente, item, pessoa)].append(e["min"])
        custos[(secao, ambiente, item, pessoa)].append(e["custo"])
        projetos[(secao, ambiente, item)].add(projeto)

    stats = defaultdict(lambda: defaultdict(dict))
    for (secao, ambiente, item), conjunto in projetos.items():
        stats[secao][(ambiente, item)]["entregas"] = len(conjunto)
    for (secao, ambiente, item, pessoa), valores in minutos.items():
        com_custo = [c for c in custos[(secao, ambiente, item, pessoa)] if c is not None]
        stats[secao][(ambiente, item)][pessoa] = {
            "n": len(valores),
            "media": statistics.mean(valores),
            "mediana": statistics.median(valores),
            "min": min(valores),
            "max": max(valores),
            "total": sum(valores),
            "custo": statistics.mean(com_custo) if com_custo else None,
        }
    return stats


def linhas_secao(secao, stats_secao):
    """
    Lista (ambiente, item, dados, fora_da_estrutura) na ordem da estrutura.
    Nas seções com itens fixos, todos aparecem mesmo sem entregas ainda.
    Em Interiores aparecem só os ambientes que já têm entregas.
    """
    linhas = []
    if secao in ESTRUTURA:
        for item in ESTRUTURA[secao]:
            linhas.append((None, item, stats_secao.get((None, item)), False))
        extras = [k for k in stats_secao if k[1] not in ESTRUTURA[secao]]
        for ambiente, item in sorted(extras, key=lambda k: list(FORA_ESTRUTURA.values()).index(k[1])
                                     if k[1] in FORA_ESTRUTURA.values() else 99):
            linhas.append((ambiente, item, stats_secao[(ambiente, item)], True))
        return linhas

    ordem_amb = [nome for nome, _ in AMBIENTES] + [SEM_AMBIENTE]
    ordem_item = ATIVIDADES_AMBIENTE + [SEM_TIPO_AMBIENTE]
    por_ambiente = sorted({k for k in stats_secao if k[0] is not None},
                          key=lambda k: (ordem_amb.index(k[0]), ordem_item.index(k[1])))
    for ambiente, item in por_ambiente:
        linhas.append((ambiente, item, stats_secao[(ambiente, item)], item == SEM_TIPO_AMBIENTE))
    extras = sorted((k for k in stats_secao if k[0] is None), key=lambda k: list(FORA_ESTRUTURA.values()).index(k[1]))
    for ambiente, item in extras:
        linhas.append((None, item, stats_secao[(ambiente, item)], True))
    return linhas


# ---------------------------------------------------------------- e-mail

ESTILO = """
body{font-family:Arial,Helvetica,sans-serif;color:#2b2b2b;font-size:14px;margin:0;padding:20px;background:#faf8f5}
.box{max-width:680px;margin:0 auto;background:#fff;border:1px solid #ece7df;border-radius:12px;padding:24px 26px}
h1{font-size:19px;margin:0 0 4px} p.sub{color:#888;margin:0 0 6px;font-size:12px;line-height:1.5}
h2{font-size:15px;margin:26px 0 6px;padding-bottom:6px;border-bottom:2px solid #c9a97c}
table{border-collapse:collapse;width:100%}
th,td{padding:6px 8px;border-bottom:1px solid #f0ece6;text-align:right;white-space:nowrap}
th{font-size:11px;color:#888;font-weight:bold;text-transform:uppercase;letter-spacing:.3px}
th:first-child,td:first-child{text-align:left;white-space:normal}
td.h{font-weight:bold;font-size:14px}
td.h small{display:block;font-weight:normal;font-size:10px;color:#aaa}
td.amb{font-weight:bold;font-size:13px;padding-top:12px;color:#7a5c33;border-bottom:1px solid #e6dccb}
td.sub{padding-left:18px}
tr.fora td{color:#999;font-style:italic} tr.fora td.h{color:#999}
tr.divisa td{border-top:1px dashed #ddd;padding-top:4px;font-size:10px;color:#aaa;text-transform:uppercase}
.muted{color:#ccc}
.pend{font-size:12px;color:#555;line-height:1.6;margin:4px 0 0;padding-left:18px}
.pend a{color:#2b5797}
.nota{color:#999;font-size:11px;line-height:1.5;margin-top:24px;border-top:1px solid #eee;padding-top:10px}
"""


def celula_media(st, entregas):
    if not st:
        return '<td class="muted">—</td>'
    base = f"<small>em {st['n']}</small>" if st["n"] != entregas else ""
    return f'<td class="h">{horas(st["media"])}{base}</td>'


def linha_html(rotulo, dados, pessoas, classes="", recuo=False):
    n = dados["entregas"] if dados else 0
    celulas = "".join(celula_media(dados.get(p) if dados else None, n) for p in pessoas)
    td = "<td class='sub'>" if recuo else "<td>"
    return f"<tr class='{classes}'>{td}{rotulo}</td><td>{n or '<span class=muted>—</span>'}</td>{celulas}</tr>"


def tabela_secao(secao, stats_secao, pessoas):
    linhas = linhas_secao(secao, stats_secao)
    if not linhas:
        return [f"<h2>{secao}</h2>", "<p class='sub'>Ainda não há entregas concluídas.</p>"]
    html = [f"<h2>{secao}</h2>", "<table><tr><th>Atividade</th><th>Entregas</th>"
            + "".join(f"<th>Média {p}</th>" for p in pessoas) + "</tr>"]
    ambiente_atual, divisa = object(), False
    for ambiente, item, dados, fora in linhas:
        if secao == INT and ambiente is not None:
            if ambiente != ambiente_atual:
                rotulo_amb = ambiente + (" (média por projeto)" if ambiente == SEM_AMBIENTE else "")
                html.append(f"<tr><td class='amb' colspan='{2 + len(pessoas)}'>{rotulo_amb}</td></tr>")
                ambiente_atual = ambiente
            html.append(linha_html(item, dados, pessoas, "fora" if fora else "", recuo=True))
            continue
        if fora and not divisa:
            html.append(f"<tr class='divisa'><td colspan='{2 + len(pessoas)}'>Fora da estrutura</td></tr>")
            divisa = True
        html.append(linha_html(item, dados, pessoas, "fora" if fora else ""))
    html.append("</table>")
    return html


def lista_pendencias(alertas, sessoes):
    """Resumo final: registros inconsistentes e descrições sem classificação, com links."""
    relevantes = [a for a in alertas if a[6] in ETAPAS]
    sem_tipo = defaultdict(list)
    for s in sessoes:
        if s["classe"] is None and s.get("etapa") in ETAPAS and s["projeto"]:
            sem_tipo[(ETAPAS[s["etapa"]], s["descricao"] or "(sem descrição)")].append(s)
    if not relevantes and not sem_tipo:
        return []

    html = ["<h2>Para corrigir no Notion</h2>"]
    if relevantes:
        html.append(f"<p class='sub'><b>Registros inconsistentes ({len(relevantes)})</b> — "
                    "os que estão fora do cálculo entram assim que forem corrigidos.</p><ul class='pend'>")
        for pessoa, desc, inicio, motivo, url, efeito, _ in relevantes:
            data = parse_iso(inicio).astimezone(FUSO).strftime("%d/%m/%y") if inicio else "sem data"
            html.append(f"<li><a href='{url}'>{data} · {pessoa} · {desc}</a> — {motivo}</li>")
        html.append("</ul>")
    if sem_tipo:
        html.append(f"<p class='sub' style='margin-top:12px'><b>Descrições sem classificação ({len(sem_tipo)})</b> — "
                    "ficam fora das médias. Ajuste a descrição no Notion ou peça para incluir uma regra.</p>"
                    "<ul class='pend'>")
        for (secao, desc), lista in sorted(sem_tipo.items(), key=lambda kv: (kv[0][0], sem_acento(kv[0][1]))):
            recente = max(lista, key=lambda s: s["ini"])
            qtd = f" ({len(lista)} registros)" if len(lista) > 1 else ""
            html.append(f"<li><a href='{recente['url']}'>{desc}</a> · {secao}{qtd}</li>")
        html.append("</ul>")
    return html


def montar_html(ate_ano, ate_mes, stats, sessoes, alertas):
    pessoas = list(PROFISSIONAIS.values())
    titulo = f"Referência de horas por atividade — até {MESES[ate_mes - 1]} de {ate_ano}"
    html = [f"<html><head><meta charset='utf-8'><style>{ESTILO}</style></head><body><div class='box'>",
            f"<h1>{titulo}</h1>",
            "<p class='sub'>Média geral acumulada de horas por entrega, com todas as entregas concluídas do "
            "Controle de Horas LSC. Cada média considera só as entregas em que aquela pessoa trabalhou "
            "(“em N” quando foram menos que o total); “—” = sem entregas.</p>"]
    for secao in (GERAL, ARQ, EXE, INT):
        html += tabela_secao(secao, stats.get(secao, {}), pessoas)
    html += lista_pendencias(alertas, sessoes)
    html.append("<p class='nota'>Entrega = todas as horas de um item dentro de um projeto. Entra no cálculo quando "
                f"o projeto é finalizado ou a etapa fica {DIAS_SEM_LANCAMENTO} dias sem lançamentos; projetos "
                "cancelados não entram. Profissional pelo Ref. Equipe, etapa pelo campo Etapa, item deduzido pela "
                "Descrição. Linhas em cinza não fazem parte da estrutura (descrição genérica) e ficam separadas "
                "para não distorcer as médias. Mediana, mínimo, máximo, horas totais e custo médio estão na "
                "planilha anexa.</p></div></body></html>")
    return titulo, "\n".join(html)


def montar_csv(stats):
    """Planilha anexa com os indicadores estatísticos (abre direto no Excel)."""
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["Etapa", "Ambiente", "Atividade", "Profissional", "Entregas", "Média (h)", "Mediana (h)",
                "Mínimo (h)", "Máximo (h)", "Horas totais", "Custo médio por entrega (R$)"])
    for secao in (GERAL, ARQ, EXE, INT):
        for ambiente, item, dados, _ in linhas_secao(secao, stats.get(secao, {})):
            for p in PROFISSIONAIS.values():
                st = (dados or {}).get(p)
                if not st:
                    continue
                w.writerow([secao, ambiente or "", item, p, st["n"], decimal_br(st["media"] / 60),
                            decimal_br(st["mediana"] / 60), decimal_br(st["min"] / 60),
                            decimal_br(st["max"] / 60), decimal_br(st["total"] / 60), decimal_br(st["custo"], 2)])
    return saida.getvalue().encode("utf-8-sig")


def enviar_email(assunto, html, planilha, nome_planilha):
    remetente = os.environ["GMAIL_USER"]
    destinatarios = [e.strip() for e in os.environ.get("EMAIL_DESTINO", remetente).split(",") if e.strip()]
    msg = MIMEMultipart("mixed")
    msg["Subject"] = assunto
    msg["From"] = remetente
    msg["To"] = ", ".join(destinatarios)
    msg.attach(MIMEText(html, "html", "utf-8"))
    anexo = MIMEApplication(planilha, Name=nome_planilha)
    anexo["Content-Disposition"] = f'attachment; filename="{nome_planilha}"'
    msg.attach(anexo)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(remetente, os.environ["GMAIL_APP_PASSWORD"])
        smtp.sendmail(remetente, destinatarios, msg.as_string())


# ---------------------------------------------------------------- main

def gerar(registros, ano, mes):
    corte = datetime(ano + (mes == 12), mes % 12 + 1, 1, tzinfo=FUSO)
    sessoes, alertas = validar(registros, corte)
    stats = calcular(entregas_concluidas(sessoes, corte))
    assunto, html = montar_html(ano, mes, stats, sessoes, alertas)
    return assunto, html, montar_csv(stats)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ate", help="último mês incluído, AAAA-MM (padrão: mês anterior)")
    parser.add_argument("--json", help="lê os registros de um arquivo JSON em vez do Notion (teste)")
    parser.add_argument("--salvar", help="salva o HTML neste arquivo (e o CSV ao lado) em vez de enviar e-mail")
    args = parser.parse_args()

    if args.ate:
        ano, mes = map(int, args.ate.split("-"))
    else:
        ultimo = datetime.now(FUSO).date().replace(day=1) - timedelta(days=1)
        ano, mes = ultimo.year, ultimo.month

    if args.json:
        with open(args.json, encoding="utf-8") as f:
            registros = json.load(f)
    else:
        registros = buscar_registros()

    assunto, html, planilha = gerar(registros, ano, mes)
    nome_planilha = f"referencia_horas_{ano}-{mes:02d}.csv"

    if args.salvar:
        with open(args.salvar, "w", encoding="utf-8") as f:
            f.write(html)
        caminho_csv = os.path.join(os.path.dirname(args.salvar), nome_planilha)
        with open(caminho_csv, "wb") as f:
            f.write(planilha)
        print(f"HTML salvo em {args.salvar} e planilha em {caminho_csv}")
    else:
        enviar_email(assunto, html, planilha, nome_planilha)
        print(f"E-mail enviado: {assunto}")
    print(f"{len(registros)} registros lidos.")


if __name__ == "__main__":
    sys.exit(main())
