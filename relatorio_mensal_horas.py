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
    python relatorio_mensal_horas.py --salvar teste.html # gera o HTML sem enviar

Variáveis de ambiente:
    NOTION_TOKEN        token da integração do Notion
    GMAIL_USER          conta Gmail que envia (ex.: liciacani@gmail.com)
    GMAIL_APP_PASSWORD  senha de app do Gmail (não é a senha normal)
    EMAIL_DESTINO       destinatários, separados por vírgula

Usa os mesmos secrets do relatorio.py e do relatorio_semanal.py, que não são alterados.
"""

import argparse
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
ARQ, EXE, INT, GERAL, TRES_D = "Projeto Arquitetônico", "Projeto Executivo", "Projeto de Interiores", "Geral", "3D"
SECOES = [GERAL, ARQ, EXE, INT, TRES_D]  # ordem no e-mail
ETAPAS = {
    "I - Projeto Arquitetônico": ARQ,
    "II - Projeto Executivo": EXE,
    "III - Projeto de Interiores": INT,
}

# ---------------------------------------------------------------- estrutura do relatório

# Itens fixos de cada seção, na ordem em que aparecem no e-mail.
PLANTA_AMBIENTADA = "Planta Baixa Ambientada"
REUNIAO = "Reunião"
REVISAO = "Revisão e Ajustes"
CORRECAO = "Correção"
LEVANTAMENTO = "Levantamento de Dados"

# 3D: todo 3D/render de qualquer etapa, organizado por tipo de ambiente.
# (nome da linha, padrão na Descrição, sem acento; o primeiro que casar vence)
TIPOS_3D = [
    ("Fachada", r"fachada|externo|muro"),
    ("Cozinha", r"cozinha|gourmet|copa|despensa|churrasqueira"),
    ("Sala", r"\bsala|estar|jantar|\btv\b|interno"),
    ("Banheiro", r"banheiro|lavabo"),
    ("Quarto", r"quarto|\bq\.? casal|bebe|hospede|suite|filh"),
    ("Área de Serviço", r"servico|\bserv\b|lavanderia"),
    ("Área Externa / Lazer", r"lazer|varanda|deck|piscina|jardim"),
    ("Closet", r"closet"),
    ("Escritório", r"escritorio|home office"),
    ("Outros ambientes", r"brinquedoteca|circulacao|\bhall\b|capela|oratorio|deposi|painel|secretaria"),
]
BASE_3D = "Base / modelagem 3D"
REVISAO_3D = "Revisão de 3D"
SEM_AMBIENTE_3D = "3D sem ambiente informado"

ESTRUTURA = {
    # Reunião = todas as reuniões, de qualquer etapa; a média é por reunião.
    GERAL: [REUNIAO, LEVANTAMENTO],
    ARQ: [PLANTA_AMBIENTADA, REVISAO, "Detalhamento", CORRECAO, "Compatibilização com Estrutural"],
    EXE: [PLANTA_AMBIENTADA, "Planta de Reforma", "Planta de Pontos",
          "Planta de Iluminação", "Planta de Gesso", "Paginação de Piso", "Detalhamento de Portas",
          "Detalhamento de Vidros", "Detalhamento de Cortinas", "Muro e Calçada",
          "Memorial de Iluminação e Pintura", CORRECAO],
    # Ambientes em ordem alfabética; base e revisão no final.
    TRES_D: sorted((nome for nome, _ in TIPOS_3D if nome != "Outros ambientes"),
                   key=lambda n: unicodedata.normalize("NFKD", n).encode("ascii", "ignore").lower())
            + [BASE_3D, REVISAO_3D],
}
# Linhas que não aparecem no relatório quando não fazem parte da estrutura da seção
# (no 3D: "Outros ambientes" e "3D sem ambiente"; no Executivo: revisão, pintura e outros detalhamentos).
OCULTOS = {"Outros ambientes", SEM_AMBIENTE_3D, REVISAO, "Planta de Pintura",
           "Outros detalhamentos (fachada, serralheria, escada, obra)",
           "Detalhamento (tipo de planta não especificado)"}
# Em Interiores, cada ambiente tem estes itens (a criação/3D fica na seção 3D).
# A correção de Interiores fica numa linha só, para a etapa toda (não por ambiente).
ATIVIDADES_AMBIENTE = ["Detalhamento de obra", "Detalhamento de marcenaria"]
# O detalhamento de obra e de marcenaria de Interiores é sempre feito pelo Willian: as horas
# contam para ele mesmo quando o registro ficou com outro Ref. Equipe.
RESPONSAVEL_DETALHAMENTO_INTERIORES = "Willian"
SEM_AMBIENTE = "Ambiente não informado"
SEM_TIPO_AMBIENTE = "Detalhamento sem tipo (obra ou marcenaria)"
TODA_ETAPA = "Toda a etapa"
# Itens de Interiores que valem para a etapa toda (não por ambiente), nesta ordem.
TODA_ETAPA_ITENS = ["Apresentação / ambientação", "Memoriais", CORRECAO]
# Itens de Interiores que não aparecem no relatório.
OCULTOS_INTERIORES = {"Cortinas"}
# Ambientes que normalmente não têm obra: detalhamento sem tipo conta como marcenaria.
SO_MARCENARIA = {"Closet", "Circulação", "Depósito", "Despensa", "Sala de Jantar", "Sala de TV / Estar",
                 "Outras salas", "Brinquedoteca"}

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
    ("Quarto", r"quarto"),
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

# Ambientes que aparecem juntos numa linha só. Cada um continua sendo uma entrega
# separada, então a média é entre os quartos (e não a soma de todos os quartos do projeto).
AGRUPAR_AMBIENTE = {
    **{nome: "Quarto" for nome in
       ("Quarto Casal", "Quarto Bebê", "Quarto Filho(a)", "Quarto Hóspedes", "Suíte", "Quarto", "Brinquedoteca")},
    **{nome: "Depósito / Despensa" for nome in ("Depósito", "Despensa")},
    **{nome: "Sala de Estar / TV" for nome in ("Sala de TV / Estar", "Outras salas")},
    **{nome: "Banheiro" for nome in ("Lavabo", "Banheiro Suíte", "Banheiro Social", "Banheiros")},
    **{nome: "Cozinha" for nome in ("Cozinha", "Copa", "Cozinha Gourmet")},
}

# Linhas que aparecem quando há horas que não cabem em nenhum item da estrutura
# (ficam separadas para não distorcer as médias dos itens).
FORA_ESTRUTURA = {
    "reuniao": REUNIAO,
    # "Projeto Executivo" / "PE" sem dizer qual planta.
    "executivo": "Detalhamento (tipo de planta não especificado)",
    "3d": SEM_AMBIENTE_3D,
    "apresentacao": "Apresentação / ambientação",
    "compatibilizacao": "Compatibilização",
    "outros_detalhes": "Outros detalhamentos (fachada, serralheria, escada, obra)",
    "memoriais": "Memoriais",
    "cortinas": "Cortinas",
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
    ambiente só é usado em Interiores e para separar entregas no 3D.

    A Descrição decide primeiro. O campo Atividade só entra quando a Descrição
    sozinha não basta (ex.: Descrição "Cozinha" + Atividade "Detalhamento").
    """
    secao = ETAPAS.get(etapa)
    if not secao:
        return None
    for texto in (descricao, f"{atividade or ''} {descricao or ''}"):
        t = sem_acento(texto)
        if t:
            classe = classificar_texto(secao, t)
            if classe:
                return classe
    return None


def classificar_texto(secao, t):
    # Geral e 3D: valem para qualquer etapa. Toda reunião (briefing, apresentação,
    # entrega...) vai para a linha Reunião da seção Geral.
    if tem(r"briefing|reuniao", t):
        return GERAL, None, REUNIAO
    if tem(r"levantamento|\bld\b|conferir medida|medicao", t):
        return GERAL, None, LEVANTAMENTO
    if tem(r"\b3d\b|render|modelagem", t):
        return classificar_3d(t, secao)

    if secao == INT:
        return classificar_interiores(t)

    if tem(r"compatibiliz", t):
        return secao, None, "Compatibilização com Estrutural" if secao == ARQ else FORA_ESTRUTURA["compatibilizacao"]

    # Apresentações e entregas (Arquitetônico e Executivo) também são reuniões.
    if tem(r"apresentac|entrega|termo|\bata\b", t):
        return GERAL, None, REUNIAO

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
        if tem(r"arquitetonic|fachada|reforma|ampliac|uso misto|legal|aprovac|^pe\b|executivo", t):
            return ARQ, None, "Detalhamento"
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
    if tem(r"fachada|serralheria|escada|pergolado|obra|marcenaria", t):
        return EXE, None, FORA_ESTRUTURA["outros_detalhes"]
    return None


def classificar_3d(t, secao):
    """3D de qualquer etapa: revisão e base/modelagem ficam separadas da criação por ambiente."""
    if tem(r"correc|revisao|ajuste|alterac", t):
        return TRES_D, None, REVISAO_3D
    if tem(r"\bbases? 3d|modelagem", t):
        return TRES_D, None, BASE_3D
    for nome, padrao in TIPOS_3D:
        if tem(padrao, t):
            # Em Quarto, Banheiro e Cozinha, guarda qual é (casal, lavabo, copa...) para cada um ser uma entrega.
            variante = (ambiente_de(t) or nome) if nome in ("Quarto", "Banheiro", "Cozinha") else None
            return TRES_D, variante, nome
    if secao == ARQ:
        return TRES_D, None, "Fachada"  # no Arquitetônico, o 3D é o da fachada
    return TRES_D, None, SEM_AMBIENTE_3D


def classificar_interiores(t):
    ambiente = ambiente_de(t)
    if tem(r"correc|revisao|ajuste|alterac", t):
        return INT, None, CORRECAO
    elif tem(r"marcenaria", t):
        item = "Detalhamento de marcenaria"
    elif tem(r"det(\.|alhamento)? de obra|\(obra\)|\bobra\b", t) and not tem(r"visita|relatorio", t):
        item = "Detalhamento de obra"
    elif tem(r"criac|ambienta|apresentac", t):
        return INT, None, FORA_ESTRUTURA["apresentacao"]
    elif tem(r"entrega|termo", t):
        return GERAL, None, REUNIAO
    elif tem(r"memoria", t):
        return INT, None, FORA_ESTRUTURA["memoriais"]
    elif tem(r"cortina", t):
        return INT, None, FORA_ESTRUTURA["cortinas"]
    elif ambiente in SO_MARCENARIA:
        item = "Detalhamento de marcenaria"  # ambiente sem obra: só o nome já indica marcenaria
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
        # Registros sem projeto em "2026 | Projetos" são desconsiderados (nem cálculo, nem alerta).
        if not r["projeto"]:
            continue
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
        if secao == GERAL and item == REUNIAO:
            ambiente = s["url"]  # cada reunião é uma entrega: a média é por reunião
        pessoa = s["pessoa"]
        if secao == INT and item in ATIVIDADES_AMBIENTE:
            pessoa = RESPONSAVEL_DETALHAMENTO_INTERIORES  # quem faz esse detalhamento, seja qual for o Ref. Equipe
        g = grupos[(s["projeto"], secao, ambiente, item, pessoa)]
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
        reuniao = secao == GERAL and item == REUNIAO  # reunião já está concluída quando acontece
        if info["status"] in STATUS_PROJETO_CANCELADO and not reuniao:
            continue
        if reuniao or info["status"] in STATUS_PROJETO_CONCLUIDO or info["ultimo"] <= limite:
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
        # Na linha do relatório, os quartos viram "Quarto" e os banheiros "Banheiro"
        # (no 3D e nas reuniões o ambiente só serve para separar as entregas). Cada um segue sendo uma entrega.
        linha = None if secao in (TRES_D, GERAL) else AGRUPAR_AMBIENTE.get(ambiente, ambiente)
        minutos[(secao, linha, item, pessoa)].append(e["min"])
        custos[(secao, linha, item, pessoa)].append(e["custo"])
        projetos[(secao, linha, item)].add((projeto, ambiente))

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
        extras = [k for k in stats_secao if k[1] not in ESTRUTURA[secao] and k[1] not in OCULTOS]
        for ambiente, item in sorted(extras, key=lambda k: list(FORA_ESTRUTURA.values()).index(k[1])
                                     if k[1] in FORA_ESTRUTURA.values() else 99):
            linhas.append((ambiente, item, stats_secao[(ambiente, item)], True))
        return linhas

    ordem_item = ATIVIDADES_AMBIENTE + [SEM_TIPO_AMBIENTE]
    # Ambientes em ordem alfabética; "Ambiente não informado" não aparece no relatório.
    por_ambiente = sorted({k for k in stats_secao if k[0] is not None and k[0] != SEM_AMBIENTE},
                          key=lambda k: (sem_acento(k[0]), ordem_item.index(k[1])))
    for ambiente, item in por_ambiente:
        linhas.append((ambiente, item, stats_secao[(ambiente, item)], item == SEM_TIPO_AMBIENTE))
    # Depois dos ambientes: os itens da etapa toda e, por último, o que está fora da estrutura.
    for item in TODA_ETAPA_ITENS:
        if (None, item) in stats_secao:
            linhas.append((TODA_ETAPA, item, stats_secao[(None, item)], False))
    extras = sorted((k for k in stats_secao if k[0] is None and k[1] not in TODA_ETAPA_ITENS
                     and k[1] not in OCULTOS_INTERIORES),
                    key=lambda k: list(FORA_ESTRUTURA.values()).index(k[1]))
    for ambiente, item in extras:
        linhas.append((None, item, stats_secao[(ambiente, item)], True))
    return linhas


# ---------------------------------------------------------------- e-mail

# Mesmo visual do relatorio.py (Relatório Diário de Produção): fonte do sistema,
# cabeçalho escuro, títulos de seção em caixa alta e tabelas com borda clara.
# Cores das pessoas = etiquetas "Aguarda Lícia" / "Aguarda Willian" do relatório diário.
# Layout compacto em duas colunas, pensado para caber em uma folha A4.
COR_PESSOA = {"Lícia": ("#92400e", "#fef3c7"), "Willian": ("#1e40af", "#dbeafe")}
NOTA_SECAO = {TRES_D: "por tipo de ambiente", INT: "detalhamento por ambiente"}
TD = "padding:4px 8px;border-bottom:1px solid #f3f3f3;font-size:11.5px;"
TH = ("font-size:9px;text-transform:uppercase;letter-spacing:.4px;color:#aaa;padding:4px 8px;"
      "border-bottom:1px solid #eee;font-weight:500;")
LINK = "color:#1a1a1a;text-decoration:none;font-weight:500;"


def etiqueta(texto, cor, fundo):
    return (f'<span style="display:inline-block;font-size:9px;padding:1px 7px;border-radius:99px;'
            f'font-weight:600;background:{fundo};color:{cor};">{texto}</span>')


def titulo_secao(numero, secao):
    nota = (f'<span style="font-weight:400;letter-spacing:0;text-transform:none;color:#aaa;"> · '
            f'{NOTA_SECAO[secao]}</span>' if secao in NOTA_SECAO else "")
    return (f'<p style="margin:0 0 6px;font-size:10px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:1.3px;color:#888;">{numero:02d} — {secao}{nota}</p>')


def celula(st):
    if not st:
        return f'<td style="{TD}text-align:right;color:#ddd;width:62px;">—</td>'
    return (f'<td style="{TD}text-align:right;white-space:nowrap;font-weight:600;color:#1a1a1a;width:62px;">'
            f'{horas(st["media"])}</td>')


def tabela(colunas, linhas):
    """colunas: rótulos do cabeçalho; linhas: (rótulo, [stats ou None...], cinza)."""
    cab = "".join(f'<th style="{TH}text-align:{"left" if i == 0 else "right"};">{c}</th>'
                  for i, c in enumerate(colunas))
    corpo = "".join(
        f'<tr><td style="{TD}color:{"#999;font-style:italic" if cinza else "#1a1a1a"};">{rotulo}</td>'
        + "".join(celula(st) for st in valores) + "</tr>"
        for rotulo, valores, cinza in linhas)
    return ('<div style="border:1px solid #eee;border-radius:6px;overflow:hidden;">'
            '<table style="width:100%;border-collapse:collapse;">'
            f'<thead><tr>{cab}</tr></thead><tbody>{corpo}</tbody></table></div>')


def cabecalho_pessoas(primeira, pessoas):
    return [primeira] + [etiqueta(p, *COR_PESSOA[p]) for p in pessoas]


def bloco_padrao(numero, secao, stats_secao, pessoas):
    """Geral, Arquitetônico, Executivo e 3D: Atividade | Lícia | Willian."""
    linhas = [(item, [(dados or {}).get(p) for p in pessoas], fora)
              for _, item, dados, fora in linhas_secao(secao, stats_secao)]
    return titulo_secao(numero, secao) + tabela(cabecalho_pessoas("Atividade", pessoas), linhas)


def bloco_interiores(numero, stats_secao, pessoas):
    """
    Interiores em matriz: uma linha por ambiente, colunas Obra | Marcenaria (médias do
    Willian, que faz todo o detalhamento). Depois, a tabela "Toda a etapa".
    """
    resp = RESPONSAVEL_DETALHAMENTO_INTERIORES
    matriz, toda = {}, []
    for ambiente, item, dados, fora in linhas_secao(INT, stats_secao):
        if ambiente == TODA_ETAPA or ambiente is None:
            toda.append((item, [(dados or {}).get(p) for p in pessoas], fora))
        elif item != SEM_TIPO_AMBIENTE:  # detalhamento sem tipo (obra ou marcenaria) não aparece
            matriz.setdefault(ambiente, {})[item] = (dados or {}).get(resp)

    html = [titulo_secao(numero, INT)]
    if not matriz and not toda:
        return "".join(html) + '<p style="color:#bbb;font-size:11px;font-style:italic;">Ainda não há entregas.</p>'
    if matriz:
        linhas = [(amb + (" (por projeto)" if amb == SEM_AMBIENTE else ""),
                   [m.get("Detalhamento de obra"), m.get("Detalhamento de marcenaria")], amb == SEM_AMBIENTE)
                  for amb, m in matriz.items()]
        html.append(tabela(["Ambiente", "Obra", "Marcenaria"], linhas))
    if toda:
        html.append('<div style="margin-top:12px;">'
                    + tabela(cabecalho_pessoas("Toda a etapa", pessoas), toda) + '</div>')
    return "".join(html)


def caixa(titulo, itens, fundo, borda):
    lis = "".join(f'<li style="margin:1px 0;">{i}</li>' for i in itens)
    return (f'<div style="background:{fundo};border-left:3px solid {borda};border-radius:0 5px 5px 0;'
            f'padding:7px 10px;font-size:10.5px;"><div style="font-weight:600;color:#1a1a1a;">{titulo}</div>'
            f'<ul style="margin:3px 0 0;padding-left:15px;color:#555;line-height:1.45;">{lis}</ul></div>')


def lista_pendencias(numero, alertas, sessoes):
    """Resumo final: registros inconsistentes e descrições sem classificação, lado a lado, com links."""
    relevantes = [a for a in alertas if a[6] in ETAPAS]
    sem_tipo = defaultdict(list)
    for s in sessoes:
        if s["classe"] is None and s.get("etapa") in ETAPAS and s["projeto"]:
            sem_tipo[(ETAPAS[s["etapa"]], s["descricao"] or "(sem descrição)")].append(s)
    if not relevantes and not sem_tipo:
        return ""

    colunas = []
    if relevantes:
        itens = []
        for pessoa, desc, inicio, motivo, url, efeito, _ in relevantes:
            data = parse_iso(inicio).astimezone(FUSO).strftime("%d/%m/%y") if inicio else "sem data"
            itens.append(f'<a href="{url}" style="{LINK}">{desc}</a> '
                         f'<span style="color:#aaa;">· {pessoa} · {data} · {motivo}</span>')
        colunas.append(caixa(f"Registros inconsistentes ({len(relevantes)})", itens, "#fffbeb", "#f6c90e"))
    if sem_tipo:
        itens = []
        for (secao, desc), lista in sorted(sem_tipo.items(), key=lambda kv: (kv[0][0], sem_acento(kv[0][1]))):
            recente = max(lista, key=lambda s: s["ini"])
            qtd = f" · {len(lista)} registros" if len(lista) > 1 else ""
            itens.append(f'<a href="{recente["url"]}" style="{LINK}">{desc}</a> '
                         f'<span style="color:#aaa;">· {secao}{qtd}</span>')
        colunas.append(caixa(f"Descrições sem classificação ({len(sem_tipo)})", itens, "#ebf4ff", "#63b3ed"))

    celulas = "".join(f'<td style="width:50%;vertical-align:top;padding:0 {"10px 0 0" if i == 0 else "0 0 10px"};">'
                      f'{c}</td>' for i, c in enumerate(colunas))
    return (f'<p style="margin:4px 0 6px;font-size:10px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:1.3px;color:#888;">{numero:02d} — Para corrigir no Notion</p>'
            f'<table style="width:100%;border-collapse:collapse;table-layout:fixed;"><tr>{celulas}</tr></table>')


def montar_html(ate_ano, ate_mes, stats, sessoes, alertas):
    pessoas = list(PROFISSIONAIS.values())
    titulo = f"Referência de horas por atividade — até {MESES[ate_mes - 1]} de {ate_ano}"
    numero = {secao: i for i, secao in enumerate(SECOES, start=1)}

    def bloco(conteudo):
        return f'<div style="margin-bottom:16px;">{conteudo}</div>'

    esquerda = "".join(bloco(bloco_padrao(numero[s], s, stats.get(s, {}), pessoas)) for s in (GERAL, ARQ, TRES_D))
    direita = (bloco(bloco_padrao(numero[EXE], EXE, stats.get(EXE, {}), pessoas))
               + bloco(bloco_interiores(numero[INT], stats.get(INT, {}), pessoas)))

    corpo = ('<table style="width:100%;border-collapse:collapse;table-layout:fixed;"><tr>'
             f'<td style="width:50%;vertical-align:top;padding-right:10px;">{esquerda}</td>'
             f'<td style="width:50%;vertical-align:top;padding-left:10px;">{direita}</td></tr></table>'
             + lista_pendencias(len(SECOES) + 1, alertas, sessoes)
             + '<p style="margin:12px 0 0;font-size:9.5px;color:#aaa;line-height:1.5;">Média de horas por entrega '
             '(todas as horas de um item em um projeto; reunião = média por reunião). Entra quando o projeto é '
             f'finalizado ou a etapa fica {DIAS_SEM_LANCAMENTO} dias sem lançamentos. Cada média considera só as '
             'entregas daquela pessoa; “—” = sem entregas. Linhas em cinza: descrição genérica.</p>')

    html = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>@page { size: A4; margin: 12mm; } @media print { body { background:#fff !important; } }</style>'
            '</head><body style="margin:0;padding:0;background:#f0f0f0;'
            'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;color:#1a1a1a;">'
            '<div style="max-width:760px;margin:24px auto;">'
            '<div style="background:#1a1a1a;padding:16px 22px;border-radius:8px 8px 0 0;">'
            '<p style="margin:0;color:#fff;font-size:17px;font-weight:600;">Referência de Horas para Orçamento</p>'
            f'<p style="margin:3px 0 0;color:#888;font-size:12px;">Acumulado até {MESES[ate_mes - 1]} de {ate_ano}</p>'
            '</div>'
            f'<div style="background:#fff;padding:18px 22px;border-radius:0 0 8px 8px;">{corpo}</div>'
            '</div></body></html>')
    return titulo, html


def enviar_email(assunto, html):
    remetente = os.environ["GMAIL_USER"]
    destinatarios = [e.strip() for e in os.environ.get("EMAIL_DESTINO", remetente).split(",") if e.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"] = remetente
    msg["To"] = ", ".join(destinatarios)
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(remetente, os.environ["GMAIL_APP_PASSWORD"])
        smtp.sendmail(remetente, destinatarios, msg.as_string())


# ---------------------------------------------------------------- main

def gerar(registros, ano, mes):
    corte = datetime(ano + (mes == 12), mes % 12 + 1, 1, tzinfo=FUSO)
    sessoes, alertas = validar(registros, corte)
    stats = calcular(entregas_concluidas(sessoes, corte))
    return montar_html(ano, mes, stats, sessoes, alertas)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ate", help="último mês incluído, AAAA-MM (padrão: mês anterior)")
    parser.add_argument("--json", help="lê os registros de um arquivo JSON em vez do Notion (teste)")
    parser.add_argument("--salvar", help="salva o HTML neste arquivo em vez de enviar e-mail")
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

    assunto, html = gerar(registros, ano, mes)

    if args.salvar:
        with open(args.salvar, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"HTML salvo em {args.salvar}")
    else:
        enviar_email(assunto, html)
        print(f"E-mail enviado: {assunto}")
    print(f"{len(registros)} registros lidos.")


if __name__ == "__main__":
    sys.exit(main())
