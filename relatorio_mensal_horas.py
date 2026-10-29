"""
Referência de horas por atividade — LSC Arquitetura

Lê TODOS os registros da base "Controle de Horas LSC" no Notion e monta a média
geral acumulada de horas por tipo de atividade, separada por profissional
(Lícia e Willian). A cada execução o cálculo é refeito do zero com todo o
histórico, então as entregas concluídas no mês entram automaticamente no
acumulado e atualizam quantidade, média, mediana, mínimo e máximo.

Uma "entrega" é a soma de todas as horas de uma atividade dentro de um projeto,
separada por profissional. Ex.: todo o Detalhamento de Marcenaria do Willian no
projeto X = 1 entrega dele.

Uso:
    python relatorio_horas.py                     # até o fim do mês anterior, envia e-mail
    python relatorio_horas.py --ate 2026-10       # até o fim de outubro de 2026
    python relatorio_horas.py --salvar teste.html # gera o HTML sem enviar

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

# Uma entrega só entra na média quando está concluída: o projeto foi finalizado
# ou a atividade ficou esse tempo sem nenhum lançamento novo naquele projeto.
STATUS_PROJETO_CONCLUIDO = {"Finalizado", "Projeto Finalizado | Execução"}
DIAS_SEM_LANCAMENTO = 30

# Registros acima disso são tratados como erro de digitação (ex.: Fim no dia errado).
DURACAO_MAXIMA_HORAS = 12

# O campo "Atividade" está vazio na base, então o tipo é deduzido pela "Descrição".
# Se a Atividade for preenchida um dia, ela tem prioridade.
# A primeira regra que casar vence, então a ordem importa.
# Os padrões são comparados sem acento e em minúsculas.
REGRAS_TIPO = [
    ("Reunião",                               r"reuniao"),
    ("Correção / Alteração",                  r"correc|alterac|ajuste|revisao"),
    ("Criação de 3D",                         r"\b3d\b|render"),
    ("Compatibilização",                      r"compatibiliz"),
    ("Memoriais",                             r"memoria"),
    ("Detalhamento de Marcenaria",            r"marcenaria"),
    ("Detalhamento de Obra",                  r"det\.? de obra|\(obra\)"),
    ("Detalhamento técnico (esquadrias, muro, cortinas…)",
                                              r"esquadria|serralheria|muro|calcada|cortina|vidro|escada|gesso|piso"),
    ("Projeto Executivo",                     r"executivo|^pe\b|pontos|iluminac"),
    ("Projeto Arquitetônico / Reforma",       r"arquitetonico|reforma|ampliac|fachada|aprovac|legal"),
    ("Ambientação / Estudo Preliminar",       r"ambienta|layout|estudo|^ep\b"),
    ("Levantamento de Dados",                 r"levantamento|^ld\b|medic"),
    ("Visita / Obra",                         r"obra|visita"),
    ("Simplific",                             r"conteudo|post|simplific|material"),
    ("Gestão / Financeiro",                   r"financeiro|orcamento|gestao|planejamento|central"),
    # Só o nome do ambiente ("Closet", "Cozinha", "Quarto Casal"...)
    ("Detalhamento por ambiente",             r"quarto|suite|banheiro|lavabo|cozinha|copa|sala|closet|varanda|"
                                              r"lazer|gourmet|servico|brinquedoteca|circulacao|deck|piscina|"
                                              r"escritorio|despensa|hall|garagem|area"),
]
SEM_TIPO = "Outros (sem classificação)"


# ---------------------------------------------------------------- utilidades

def sem_acento(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c)).lower().strip()


def classificar(atividade, descricao):
    if atividade:
        return atividade
    alvo = sem_acento(descricao)
    for tipo, padrao in REGRAS_TIPO:
        if re.search(padrao, alvo):
            return tipo
    return SEM_TIPO


def id_pagina(url_ou_id):
    """Extrai o id de 32 caracteres de uma URL ou id do Notion."""
    achado = re.findall(r"[0-9a-f]{32}", (url_ou_id or "").replace("-", ""))
    return achado[-1] if achado else None


def parse_iso(valor):
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


def horas(minutos):
    if minutos is None:
        return "—"
    minutos = int(round(minutos))
    return f"{minutos // 60}h{minutos % 60:02d}"


def reais(valor):
    if valor is None:
        return "—"
    return "R$ " + f"{valor:,.0f}".replace(",", ".")


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
            equipe = ids_relacao(p.get("Ref. Equipe"))
            registros.append({
                "url": pagina["url"],
                "criado_por": (p.get("Criado por") or {}).get("created_by", {}).get("id"),
                "equipe": equipe,
                "projeto": projetos[0] if projetos else None,
                "status_projeto": status_rollup(p.get("Status do Projeto")),
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
    """
    alertas = []
    por_pessoa = defaultdict(list)

    for r in registros:
        if r["inicio"] and parse_iso(r["inicio"]) >= corte:
            continue
        rotulo = r["descricao"] or "(sem descrição)"
        equipe = [e for e in r["equipe"] if e in PROFISSIONAIS]
        if len(equipe) != 1:
            alertas.append(("—", rotulo, r["inicio"], "Ref. Equipe vazia ou com mais de uma pessoa", r["url"], "fora"))
            continue
        pessoa_id = equipe[0]
        pessoa = PROFISSIONAIS[pessoa_id]

        if not r["inicio"] or not r["fim"]:
            alertas.append((pessoa, rotulo, r["inicio"], "sem horário de início ou de fim", r["url"], "fora"))
            continue
        inicio, fim = parse_iso(r["inicio"]), parse_iso(r["fim"])
        minutos = (fim - inicio).total_seconds() / 60
        if minutos <= 0:
            alertas.append((pessoa, rotulo, r["inicio"], "fim antes do início", r["url"], "fora"))
            continue
        if minutos > DURACAO_MAXIMA_HORAS * 60:
            alertas.append((pessoa, rotulo, r["inicio"],
                            f"{horas(minutos)} seguidas (fim provavelmente no dia errado)", r["url"], "fora"))
            continue
        if r["criado_por"] and USUARIOS.get(r["criado_por"]) not in (None, pessoa_id):
            alertas.append((pessoa, rotulo, r["inicio"],
                            "lançado por outra pessoa — conferir se as horas são mesmo de " + pessoa,
                            r["url"], "contado"))

        por_pessoa[pessoa].append({**r, "pessoa": pessoa, "ini": inicio, "fim_dt": fim, "min": minutos})

    sessoes = []
    for pessoa, lista in por_pessoa.items():
        lista.sort(key=lambda s: (s["ini"], s["fim_dt"]))
        vistos = set()
        maior_fim, dono_maior_fim = None, None
        for s in lista:
            rotulo = s["descricao"] or "(sem descrição)"
            chave = (s["ini"], s["fim_dt"])
            if chave in vistos:
                alertas.append((pessoa, rotulo, s["inicio"], "duplicado (mesmo início e fim de outro registro)",
                                s["url"], "fora"))
                continue
            vistos.add(chave)

            contar_de = s["ini"]
            if maior_fim and s["ini"] < maior_fim:
                contar_de = min(maior_fim, s["fim_dt"])
                repetido = (contar_de - s["ini"]).total_seconds() / 60
                alertas.append((pessoa, rotulo, s["inicio"],
                                f"sobreposto a “{dono_maior_fim}” — {horas(repetido)} contadas só uma vez",
                                s["url"], "parcial"))
            minutos = (s["fim_dt"] - contar_de).total_seconds() / 60
            if not maior_fim or s["fim_dt"] > maior_fim:
                maior_fim, dono_maior_fim = s["fim_dt"], rotulo
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
    Agrupa as sessões em entregas (projeto + atividade + profissional) e devolve
    só as concluídas até a data de corte.
    """
    grupos = defaultdict(lambda: {"min": 0.0, "custo": 0.0, "sem_custo": False, "ultimo": None, "status": None})
    for s in sessoes:
        if not s["projeto"]:
            continue
        g = grupos[(s["projeto"], s["tipo"], s["pessoa"])]
        g["min"] += s["min"]
        if s["custo"] is None:
            g["sem_custo"] = True
        else:
            g["custo"] += s["custo"]
        g["ultimo"] = max(g["ultimo"] or s["fim_dt"], s["fim_dt"])
        g["status"] = g["status"] or s["status_projeto"]

    # A conclusão é decidida por projeto + atividade (vale para os dois profissionais).
    ultimo_atividade = defaultdict(lambda: None)
    for (projeto, tipo, _), g in grupos.items():
        atual = ultimo_atividade[(projeto, tipo)]
        ultimo_atividade[(projeto, tipo)] = max(atual or g["ultimo"], g["ultimo"])

    limite = corte - timedelta(days=DIAS_SEM_LANCAMENTO)
    concluidas = {}
    for (projeto, tipo, pessoa), g in grupos.items():
        finalizado = g["status"] in STATUS_PROJETO_CONCLUIDO
        parado = ultimo_atividade[(projeto, tipo)] <= limite
        if finalizado or parado:
            concluidas[(projeto, tipo, pessoa)] = {"min": g["min"],
                                                   "custo": None if g["sem_custo"] else g["custo"]}
    return concluidas


def estatisticas(valores, custos):
    custos = [c for c in custos if c is not None]
    return {
        "n": len(valores),
        "media": statistics.mean(valores),
        "mediana": statistics.median(valores),
        "min": min(valores),
        "max": max(valores),
        "custo": statistics.mean(custos) if custos else None,
    }


def calcular(entregas):
    """stats[tipo][pessoa ou 'Total'] = estatísticas das entregas concluídas."""
    por_pessoa = defaultdict(lambda: defaultdict(lambda: ([], [])))
    total = defaultdict(lambda: {"min": 0.0, "custo": 0.0, "sem_custo": False})
    for (projeto, tipo, pessoa), e in entregas.items():
        minutos, custos = por_pessoa[tipo][pessoa]
        minutos.append(e["min"])
        custos.append(e["custo"])
        t = total[(projeto, tipo)]
        t["min"] += e["min"]
        if e["custo"] is None:
            t["sem_custo"] = True
        else:
            t["custo"] += e["custo"]

    stats = defaultdict(dict)
    for tipo, pessoas in por_pessoa.items():
        for pessoa, (minutos, custos) in pessoas.items():
            stats[tipo][pessoa] = estatisticas(minutos, custos)
    totais = defaultdict(lambda: ([], []))
    for (projeto, tipo), t in total.items():
        totais[tipo][0].append(t["min"])
        totais[tipo][1].append(None if t["sem_custo"] else t["custo"])
    for tipo, (minutos, custos) in totais.items():
        stats[tipo]["Total"] = estatisticas(minutos, custos)
    return stats


# ---------------------------------------------------------------- e-mail

ESTILO = """
body{font-family:Arial,Helvetica,sans-serif;color:#2b2b2b;font-size:14px;margin:0;padding:24px;background:#faf8f5}
.box{max-width:820px;margin:0 auto;background:#fff;border:1px solid #ece7df;border-radius:12px;padding:28px}
h1{font-size:21px;margin:0 0 4px} h2{font-size:16px;margin:32px 0 6px}
p.sub{color:#777;margin:0 0 16px;font-size:13px}
.kpis{width:100%;border-collapse:separate;border-spacing:8px 0;margin:20px -8px 4px}
.kpis td{background:#f4f1ec;border-radius:10px;padding:12px 14px;text-align:left;border:0}
.kpis b{display:block;font-size:22px;color:#2b2b2b} .kpis span{color:#777;font-size:12px}
table.t{border-collapse:collapse;width:100%}
.t th,.t td{padding:8px 10px;border-bottom:1px solid #eee;text-align:right;white-space:nowrap}
.t th{font-size:12px;color:#777;font-weight:bold;border-bottom:2px solid #e6e0d6}
.t th:first-child,.t td:first-child{text-align:left;white-space:normal}
.t td.ref{font-weight:bold;font-size:15px}
.n{color:#999;font-size:11px;font-weight:normal;display:block}
.novo{color:#2f7d4f;font-size:11px}
.muted{color:#bbb}
.bar{background:#c9a97c;height:6px;border-radius:3px;display:inline-block;vertical-align:middle}
.alerta td{font-size:12px} .alerta td:first-child{white-space:nowrap}
.tag{font-size:11px;padding:2px 6px;border-radius:4px;background:#fdecea;color:#a33}
.tag.p{background:#fff4e0;color:#8a5a00} .tag.c{background:#eef3fb;color:#2b5797}
.nota{color:#888;font-size:12px;line-height:1.5;margin-top:28px;border-top:1px solid #eee;padding-top:14px}
"""


def celula_ref(st):
    if not st:
        return '<td class="muted">—</td>'
    return f'<td class="ref">{horas(st["media"])}<span class="n">{st["n"]} entrega{"s" * (st["n"] != 1)}</span></td>'


def montar_html(ate_ano, ate_mes, stats, stats_antes, sessoes, alertas, sem_projeto):
    pessoas = list(PROFISSIONAIS.values())
    titulo = f"Referência de horas por atividade — atualizada até {MESES[ate_mes - 1]} de {ate_ano}"
    tipos = sorted(stats, key=lambda t: -stats[t]["Total"]["n"])
    tipos = [t for t in tipos if t != SEM_TIPO] + ([SEM_TIPO] if SEM_TIPO in stats else [])

    n_entregas = sum(stats[t]["Total"]["n"] for t in stats)
    n_antes = sum(stats_antes[t]["Total"]["n"] for t in stats_antes)
    primeiro = min(s["ini"] for s in sessoes).astimezone(FUSO) if sessoes else None
    corrigir = sum(1 for a in alertas if a[5] != "contado")

    html = [f"<html><head><meta charset='utf-8'><style>{ESTILO}</style></head><body><div class='box'>",
            f"<h1>{titulo}</h1>",
            "<p class='sub'>Média geral acumulada de todas as entregas concluídas registradas no "
            "Controle de Horas LSC" + (f", desde {MESES[primeiro.month - 1]} de {primeiro.year}" if primeiro else "")
            + ". Use como referência para orçamentos.</p>",
            "<table class='kpis'><tr>",
            f"<td><b>{n_entregas}</b><span>entregas na base de referência</span></td>",
            f"<td><b>+{n_entregas - n_antes}</b><span>entregas incorporadas neste mês</span></td>",
            f"<td><b>{horas(sum(s['min'] for s in sessoes))}</b><span>horas válidas registradas</span></td>",
            f"<td><b>{corrigir}</b><span>registros para corrigir</span></td>",
            "</tr></table>"]

    # Tabela de referência
    maior = max((stats[t]["Total"]["media"] for t in tipos), default=1)
    html.append("<h2>Tabela de referência — média por entrega</h2>"
                "<p class='sub'>Horas médias que cada profissional gasta em uma entrega daquela atividade "
                "(todas as sessões dela dentro de um projeto). “—” = o profissional não participa da atividade.</p>"
                "<table class='t'><tr><th>Atividade</th>"
                + "".join(f"<th>{p}</th>" for p in pessoas)
                + "<th>Total</th><th>Custo médio</th><th></th></tr>")
    for t in tipos:
        tot = stats[t]["Total"]
        barra = int(110 * tot["media"] / maior) if maior else 0
        html.append(f"<tr><td>{t}</td>"
                    + "".join(celula_ref(stats[t].get(p)) for p in pessoas)
                    + celula_ref(tot)
                    + f"<td>{reais(tot['custo'])}</td>"
                    f"<td><span class='bar' style='width:{barra}px'></span></td></tr>")
    html.append("</table>")

    # Detalhe por profissional
    for p in pessoas:
        linhas = [t for t in tipos if p in stats[t]]
        if not linhas:
            continue
        html.append(f"<h2>Detalhe — {p}</h2><table class='t'><tr><th>Atividade</th><th>Entregas</th>"
                    "<th>Média</th><th>Mediana</th><th>Mínimo</th><th>Máximo</th><th>Custo médio</th></tr>")
        for t in linhas:
            st = stats[t][p]
            novas = st["n"] - stats_antes.get(t, {}).get(p, {}).get("n", 0)
            html.append(f"<tr><td>{t}</td>"
                        f"<td>{st['n']}" + (f" <span class='novo'>+{novas}</span>" if novas > 0 else "") + "</td>"
                        f"<td><b>{horas(st['media'])}</b></td><td>{horas(st['mediana'])}</td>"
                        f"<td>{horas(st['min'])}</td><td>{horas(st['max'])}</td>"
                        f"<td>{reais(st['custo'])}</td></tr>")
        html.append("</table>")

    # Alertas
    if alertas:
        tags = {"fora": ("", "não contado"), "parcial": ("p", "contado em parte"), "contado": ("c", "conferir")}
        html.append(f"<h2>Registros para conferir ({len(alertas)})</h2>"
                    "<p class='sub'>Corrija no Notion; no próximo relatório eles entram no cálculo.</p>"
                    "<table class='t alerta'><tr><th>Data</th><th>Profissional</th><th>Descrição</th>"
                    "<th style='text-align:left'>Problema</th><th></th></tr>")
        for pessoa, desc, inicio, motivo, url, efeito in alertas:
            data = parse_iso(inicio).astimezone(FUSO).strftime("%d/%m/%y") if inicio else "—"
            cls, rotulo = tags[efeito]
            html.append(f"<tr><td>{data}</td><td>{pessoa}</td><td style='text-align:left'>{desc}</td>"
                        f"<td style='text-align:left;white-space:normal'>{motivo} "
                        f"<span class='tag {cls}'>{rotulo}</span></td>"
                        f"<td><a href='{url}'>abrir</a></td></tr>")
        html.append("</table>")

    nao_classificadas = sorted({s["descricao"] or "(sem descrição)" for s in sessoes if s["tipo"] == SEM_TIPO})
    html.append("<div class='nota'><b>Como os números são calculados.</b> "
                "Profissional = Ref. Equipe. Custo = fórmula Custo da base (custo-hora de cada profissional). "
                "Tipo de atividade deduzido pela Descrição. Uma entrega entra na média quando o projeto é "
                f"finalizado ou quando a atividade fica {DIAS_SEM_LANCAMENTO} dias sem lançamentos naquele projeto. "
                "Registros sobrepostos têm o trecho repetido contado uma vez só; duplicados e inconsistentes "
                "ficam de fora até serem corrigidos."
                + (f"<br><br>{horas(sem_projeto)} registradas sem projeto vinculado não entram na média por entrega "
                   "(não é possível saber a que entrega pertencem)." if sem_projeto else "")
                + ("<br><br>Descrições sem classificação: " + ", ".join(nao_classificadas[:30])
                   if nao_classificadas else "")
                + "</div></div></body></html>")
    return titulo, "\n".join(html)


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
    corte_antes = datetime(ano, mes, 1, tzinfo=FUSO)

    sessoes, alertas = validar(registros, corte)
    stats = calcular(entregas_concluidas(sessoes, corte))
    sessoes_antes = [s for s in sessoes if s["ini"] < corte_antes]
    stats_antes = calcular(entregas_concluidas(sessoes_antes, corte_antes))
    sem_projeto = sum(s["min"] for s in sessoes if not s["projeto"])
    return montar_html(ano, mes, stats, stats_antes, sessoes, alertas, sem_projeto)


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
