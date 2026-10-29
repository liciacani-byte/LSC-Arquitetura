"""
Referência de horas por atividade — LSC Arquitetura

Lê TODOS os registros da base "Controle de Horas LSC" no Notion e monta a média
geral acumulada de horas por etapa (Arquitetônico, Executivo, Interiores),
atividade e profissional (Lícia e Willian), para usar em orçamentos. A cada
execução o cálculo é refeito do zero com todo o histórico, então as entregas
concluídas entram automaticamente no acumulado.

Uma "entrega" é a soma de todas as horas de uma atividade dentro de uma etapa de
um projeto, separada por profissional. Ex.: todo o Detalhamento de Marcenaria do
Willian na etapa Interiores do projeto X = 1 entrega dele.

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

# Etapas mostradas no relatório (valor do campo Etapa -> título da seção), nesta ordem.
ETAPAS = {
    "I - Projeto Arquitetônico": "Arquitetônico",
    "II - Projeto Executivo": "Executivo",
    "III - Projeto de Interiores": "Interiores",
}

# Uma entrega só entra na média quando a etapa daquele projeto terminou: o projeto
# foi finalizado, ou a etapa inteira ficou esse tempo sem nenhum lançamento.
# Projetos cancelados ficam de fora (o trabalho foi interrompido no meio).
STATUS_PROJETO_CONCLUIDO = {"Finalizado", "Projeto Finalizado | Execução"}
STATUS_PROJETO_CANCELADO = {"Cancelado"}
DIAS_SEM_LANCAMENTO = 30

# Registros acima disso são tratados como erro de digitação (ex.: Fim no dia errado).
DURACAO_MAXIMA_HORAS = 12

# Se o campo "Atividade" for preenchido um dia, ele tem prioridade sobre a Descrição.
# "Detalhamento" é genérico demais, então nesse caso a Descrição ainda decide.
ATIVIDADE_NOTION = {
    "3D / Render": "Criação de 3D",
    "Compatibilização": "Compatibilização",
    "Correção": "Correção / Alteração",
    "Reunião": "Reunião",
    "Visita Técnica": "Visita / Obra",
    "Financeiro": "Gestão / Financeiro",
    "Planejamento": "Gestão / Financeiro",
}

# Como o campo "Atividade" está vazio na base, o tipo é deduzido pela "Descrição".
# A primeira regra que casar vence, então a ordem importa.
# Os padrões são comparados sem acento e em minúsculas.
REGRAS_TIPO = [
    ("Reunião",                         r"reuniao"),
    # Retrabalho fica separado da produção, em qualquer atividade.
    ("Correção / Alteração",            r"correc|alterac|ajuste|revisao"),
    ("Compatibilização",                r"compatibiliz"),
    ("Apresentação / Entrega",          r"apresentac|entrega|termo"),
    # Base/modelagem (montar o modelo) é diferente de 3D de ambiente (criação e render).
    ("Modelagem 3D (base)",             r"\bbases? 3d|modelagem"),
    ("Criação de 3D",                   r"\b3d\b|render"),
    ("Memoriais",                       r"memoria"),
    ("Detalhamento de Marcenaria",      r"marcenaria"),
    ("Detalhamento de Obra",            r"det(\.|alhamento)? de obra|\(obra\)"),
    ("Plantas técnicas (pontos, iluminação, gesso, piso)",
                                        r"pontos|iluminac|gesso|piso"),
    ("Detalhamento técnico (esquadrias, muro, serralheria, cortinas)",
                                        r"esquadria|serralheria|muro|calcada|cortina|vidro|escada|porta|portao|portoes|"
                                        r"pergolado"),
    ("Projeto Executivo (geral)",       r"executivo|^pe\b"),
    ("Projeto Arquitetônico / Reforma", r"arquitetonic|reforma|ampliac|fachada|aprovac|legal|uso misto"),
    ("Ambientação / Estudo Preliminar", r"ambienta|layout|estudo|^ep\b|^criacao"),
    ("Levantamento de Dados",           r"levantamento|^ld\b|medic"),
    ("Visita / Obra",                   r"obra|visita"),
    ("Simplific",                       r"conteudo|post|simplific|material|site|combos"),
    ("Gestão / Financeiro",             r"financeiro|orcamento|gestao|planejamento|central|boleto|\bata\b"),
    # Só o nome do ambiente ("Closet", "Cozinha", "Quarto Casal"...), sem dizer se é obra ou marcenaria.
    ("Detalhamento por ambiente (obra e marcenaria)",
                                        r"quarto|suite|banheiro|lavabo|cozinha|copa|sala|closet|varanda|"
                                        r"lazer|gourmet|servico|brinquedoteca|circulacao|deck|piscina|"
                                        r"escritorio|despensa|deposi|oratorio|hall|garagem|area"),
]
SEM_TIPO = "Sem classificação"


# ---------------------------------------------------------------- utilidades

def sem_acento(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c)).lower().strip()


def classificar(atividade, descricao):
    if atividade in ATIVIDADE_NOTION:
        return ATIVIDADE_NOTION[atividade]
    alvo = sem_acento(descricao)
    for tipo, padrao in REGRAS_TIPO:
        if re.search(padrao, alvo):
            return tipo
    return SEM_TIPO


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
                            "tipo": classificar(s["atividade"], s["descricao"])})

    alertas.sort(key=lambda a: a[2] or "", reverse=True)
    return sessoes, alertas


# ---------------------------------------------------------------- cálculo

def entregas_concluidas(sessoes, corte):
    """
    Agrupa as sessões em entregas (projeto + etapa + atividade + profissional) e
    devolve só as de etapas concluídas até a data de corte.

    A conclusão é decidida pela etapa do projeto inteira, não por atividade: assim
    uma atividade que só ficou parada (esperando o cliente, por exemplo) não entra
    na média pela metade enquanto o resto da etapa continua.
    """
    grupos = defaultdict(lambda: {"min": 0.0, "custo": 0.0, "sem_custo": False})
    etapa_info = defaultdict(lambda: {"ultimo": None, "status": None})
    for s in sessoes:
        if not s["projeto"] or s.get("etapa") not in ETAPAS or s["tipo"] == SEM_TIPO:
            continue
        g = grupos[(s["projeto"], s["etapa"], s["tipo"], s["pessoa"])]
        g["min"] += s["min"]
        if s["custo"] is None:
            g["sem_custo"] = True
        else:
            g["custo"] += s["custo"]
        info = etapa_info[(s["projeto"], s["etapa"])]
        info["ultimo"] = max(info["ultimo"] or s["fim_dt"], s["fim_dt"])
        info["status"] = info["status"] or s["status_projeto"]

    limite = corte - timedelta(days=DIAS_SEM_LANCAMENTO)
    concluidas = {}
    for (projeto, etapa, tipo, pessoa), g in grupos.items():
        info = etapa_info[(projeto, etapa)]
        if info["status"] in STATUS_PROJETO_CANCELADO:
            continue
        if info["status"] in STATUS_PROJETO_CONCLUIDO or info["ultimo"] <= limite:
            concluidas[(projeto, etapa, tipo, pessoa)] = {"min": g["min"],
                                                          "custo": None if g["sem_custo"] else g["custo"]}
    return concluidas


def calcular(entregas):
    """
    Devolve stats[etapa][tipo] com:
      "entregas": nº de projetos com aquela atividade concluída na etapa
      pessoa -> estatísticas só das entregas daquela pessoa
    """
    minutos = defaultdict(list)
    custos = defaultdict(list)
    projetos = defaultdict(set)
    for (projeto, etapa, tipo, pessoa), e in entregas.items():
        minutos[(etapa, tipo, pessoa)].append(e["min"])
        custos[(etapa, tipo, pessoa)].append(e["custo"])
        projetos[(etapa, tipo)].add(projeto)

    stats = defaultdict(lambda: defaultdict(dict))
    for (etapa, tipo), conjunto in projetos.items():
        stats[etapa][tipo]["entregas"] = len(conjunto)
    for (etapa, tipo, pessoa), valores in minutos.items():
        com_custo = [c for c in custos[(etapa, tipo, pessoa)] if c is not None]
        stats[etapa][tipo][pessoa] = {
            "n": len(valores),
            "media": statistics.mean(valores),
            "mediana": statistics.median(valores),
            "min": min(valores),
            "max": max(valores),
            "total": sum(valores),
            "custo": statistics.mean(com_custo) if com_custo else None,
        }
    return stats


def ordem_tipos(stats_etapa):
    return sorted(stats_etapa, key=lambda t: (-stats_etapa[t]["entregas"], t))


# ---------------------------------------------------------------- e-mail

ESTILO = """
body{font-family:Arial,Helvetica,sans-serif;color:#2b2b2b;font-size:14px;margin:0;padding:20px;background:#faf8f5}
.box{max-width:680px;margin:0 auto;background:#fff;border:1px solid #ece7df;border-radius:12px;padding:24px 26px}
h1{font-size:19px;margin:0 0 4px} p.sub{color:#888;margin:0 0 6px;font-size:12px;line-height:1.5}
h2{font-size:15px;margin:26px 0 6px;padding-bottom:6px;border-bottom:2px solid #c9a97c}
table{border-collapse:collapse;width:100%}
th,td{padding:7px 8px;border-bottom:1px solid #f0ece6;text-align:right;white-space:nowrap}
th{font-size:11px;color:#888;font-weight:bold;text-transform:uppercase;letter-spacing:.3px}
th:first-child,td:first-child{text-align:left;white-space:normal}
td.h{font-weight:bold;font-size:15px}
td.h small{display:block;font-weight:normal;font-size:10px;color:#aaa}
.muted{color:#ccc}
.vazio{color:#999;font-size:13px;margin:4px 0 0}
.pend{font-size:12px;color:#555;line-height:1.6;margin:4px 0 0;padding-left:18px}
.pend a{color:#2b5797}
.nota{color:#999;font-size:11px;line-height:1.5;margin-top:24px;border-top:1px solid #eee;padding-top:10px}
"""


def celula_media(st, entregas):
    if not st:
        return '<td class="muted">—</td>'
    base = f"<small>em {st['n']}</small>" if st["n"] != entregas else ""
    return f'<td class="h">{horas(st["media"])}{base}</td>'


def tabela_etapa(nome, stats_etapa, pessoas):
    html = [f"<h2>{nome}</h2>"]
    tipos = ordem_tipos(stats_etapa)
    if not tipos:
        html.append("<p class='vazio'>Ainda não há entregas concluídas nesta etapa.</p>")
        return html
    html.append("<table><tr><th>Atividade</th><th>Entregas</th>"
                + "".join(f"<th>Média {p}</th>" for p in pessoas) + "</tr>")
    for t in tipos:
        n = stats_etapa[t]["entregas"]
        html.append(f"<tr><td>{t}</td><td>{n}</td>"
                    + "".join(celula_media(stats_etapa[t].get(p), n) for p in pessoas) + "</tr>")
    html.append("</table>")
    return html


def lista_pendencias(alertas, sessoes):
    """Resumo final: registros inconsistentes e descrições sem classificação, com links."""
    relevantes = [a for a in alertas if a[6] in ETAPAS]
    sem_tipo = defaultdict(list)
    for s in sessoes:
        if s["tipo"] == SEM_TIPO and s.get("etapa") in ETAPAS:
            sem_tipo[s["descricao"] or "(sem descrição)"].append(s)
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
        for desc, lista in sorted(sem_tipo.items(), key=lambda kv: sem_acento(kv[0])):
            recente = max(lista, key=lambda s: s["ini"])
            qtd = f" ({len(lista)} registros)" if len(lista) > 1 else ""
            html.append(f"<li><a href='{recente['url']}'>{desc}</a>{qtd}</li>")
        html.append("</ul>")
    return html


def montar_html(ate_ano, ate_mes, stats, sessoes, alertas):
    pessoas = list(PROFISSIONAIS.values())
    titulo = f"Referência de horas por atividade — até {MESES[ate_mes - 1]} de {ate_ano}"
    html = [f"<html><head><meta charset='utf-8'><style>{ESTILO}</style></head><body><div class='box'>",
            f"<h1>{titulo}</h1>",
            "<p class='sub'>Média geral acumulada de horas por entrega, com todas as entregas concluídas do "
            "Controle de Horas LSC. Cada média considera só as entregas em que aquela pessoa trabalhou "
            "(“em N” quando foram menos que o total); “—” = não participa da atividade.</p>"]
    for etapa, nome in ETAPAS.items():
        html += tabela_etapa(nome, stats.get(etapa, {}), pessoas)
    html += lista_pendencias(alertas, sessoes)
    html.append("<p class='nota'>Entrega = todas as horas de uma atividade dentro da etapa de um projeto. "
                f"Entra no cálculo quando o projeto é finalizado ou a etapa fica {DIAS_SEM_LANCAMENTO} dias "
                "sem lançamentos; projetos cancelados não entram. Profissional pelo Ref. Equipe, etapa pelo campo "
                "Etapa, atividade deduzida pela Descrição. Mediana, mínimo, máximo, horas totais e custo médio "
                "estão na planilha anexa.</p></div></body></html>")
    return titulo, "\n".join(html)


def montar_csv(stats):
    """Planilha anexa com os indicadores estatísticos (abre direto no Excel)."""
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["Etapa", "Atividade", "Profissional", "Entregas", "Média (h)", "Mediana (h)",
                "Mínimo (h)", "Máximo (h)", "Horas totais", "Custo médio por entrega (R$)"])
    for etapa, nome in ETAPAS.items():
        stats_etapa = stats.get(etapa, {})
        for t in ordem_tipos(stats_etapa):
            for p in PROFISSIONAIS.values():
                st = stats_etapa[t].get(p)
                if not st:
                    continue
                w.writerow([nome, t, p, st["n"], decimal_br(st["media"] / 60), decimal_br(st["mediana"] / 60),
                            decimal_br(st["min"] / 60), decimal_br(st["max"] / 60), decimal_br(st["total"] / 60),
                            decimal_br(st["custo"], 2)])
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
